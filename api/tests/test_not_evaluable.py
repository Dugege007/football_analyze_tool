"""N5/N5-PIN「不可评估」记账（口径卡 v1 §6）+ 已有策略 validate 数字回归。

- 回归：tests/fixtures/validate_baseline_pre_not_evaluable.json 是改动前代码在冻结副本
  tests/fixtures/regress_db/{prod,v2d3}.db 上算出的 summary + 逐行 row_fingerprint
  （S1/S2/S8/N1/N4/V3/TEST_V3_MIRROR × scope{jingcai,all} × settlement_version×2 × book{macau,crown}）。
  改动后除新增的不可评估键外必须逐键相等。
- 单测：在冻结副本的临时拷贝上插入不可评估预测，检查 n_eligible 不含它们、by_reason 计数正确。
全部只读现网；临时库在 tmp_path。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import backtest as bt  # noqa: E402
from app import main as m  # noqa: E402
from app import shadow_evaluable as se  # noqa: E402

# 0.3.18：N5 / N5-PIN 快照必须带 ρ 字段 + 指纹 rho / rho_se_method（shadow_evaluable._check_n5_rho）
N5RHO = {"rho_global": -0.05, "rho_league": -0.04, "rho_league_se": 0.03,
         "rho_Q": 4.1, "rho_df": 6, "rho_I2": 0.0, "rho_Q_p": 0.66}  # 0.3.19 每竞彩日 Q/df/I2/Q_p
N5FP = {"rho": "global_pooled_ivw", "rho_se_method": "profile_kish", "rho_pool": "fe"}


def _b5(**kw):
    """N5 / N5-PIN 快照：未给时补 ρ 字段与指纹 rho / rho_se_method（0.3.18 强制）。"""
    if kw.get("ledger_id") in ("N5", "N5-PIN"):
        for k, v in N5RHO.items():
            kw.setdefault(k, v)
        kw["fingerprint"] = {**N5FP, **(kw.get("fingerprint") or {})}
    return se.build_snapshot(**kw)

import regress_validate_capture as cap  # noqa: E402

BASELINE = ROOT / "tests" / "fixtures" / "validate_baseline_pre_not_evaluable.json"
NEW_KEYS = {"n_not_evaluable", "n_not_evaluable_by_reason", "n_not_evaluable_by_subreason",
            "not_evaluable_items", "by_odds_source", "odds_source_split",
            "by_open_basis", "open_basis_split",
            "by_close_basis", "close_basis_split",  # 0.3.18 C
            "n_postpone_pending", "n_void_postponed", "postpone_items", "postpone_void_hours"}  # 0.3.19
PROD_DB = ROOT / "data" / "app.db"
V2D3_DB = ROOT / "data" / "v2d3" / "app.db"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# 只看本模块可能写的表（odds 表有其它任务在并发回补，整库 sha 会抖）
_WATCH = ("strategy_defs", "strategy_validation_runs", "strategy_validation_cache", "predictions")


def _ledger_sig(p: Path) -> dict:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {t}").fetchone() for t in _WATCH}
    finally:
        c.close()


@pytest.fixture(scope="module")
def live_sha_before():
    return {p: _ledger_sig(p) for p in (PROD_DB, V2D3_DB)}


def test_regression_existing_strategies_unchanged(live_sha_before):
    before = json.loads(BASELINE.read_text())
    after = cap.capture()
    assert set(before) == set(after)
    assert len(before) == 112
    for k in before:
        b, a = before[k], after[k]
        assert a["row_fingerprints"] == b["row_fingerprints"], k
        a_sum = {kk: vv for kk, vv in a["summary"].items() if kk not in NEW_KEYS}
        assert a_sum == b["summary"], k
        assert a["summary"]["n_not_evaluable"] == 0, k
        assert a["summary"]["n_not_evaluable_by_reason"] == {r: 0 for r in se.REASONS}, k
        # 来源分账：现有策略全部落 unknown，且 unknown 与顶层逐键一致；live/hist 为空
        assert a["summary"]["odds_source_split"] is True, k
        bos = a["summary"]["by_odds_source"]
        assert set(bos) == {"live", "hist", "unknown"}, k
        for kk, vv in bos["unknown"].items():
            if kk in b["summary"]:
                assert vv == b["summary"][kk], (k, kk)
        for src in ("live", "hist"):
            assert bos[src]["n_eligible"] == 0 and bos[src]["hits"] == 0, (k, src)
            assert bos[src]["coverage"] is None and bos[src]["n_not_evaluable"] == 0, (k, src)
        # 初盘口径分账：同样全部 unknown
        assert a["summary"]["open_basis_split"] is True, k
        bob = a["summary"]["by_open_basis"]
        assert set(bob) == {"first_tick", "api_opening", "unknown"}, k
        for kk, vv in bob["unknown"].items():
            if kk in b["summary"]:
                assert vv == b["summary"][kk], (k, kk)
        for ob in ("first_tick", "api_opening"):
            assert bob[ob]["n_eligible"] == 0 and bob[ob]["hits"] == 0, (k, ob)
            assert bob[ob]["coverage"] is None and bob[ob]["n_not_evaluable"] == 0, (k, ob)
    # 抽查几个非平凡数字（防止基线本身是空的）
    v3 = after["prod|CFFXDJ_5_V3|jingcai|ah_v4_water_midpoint|macau_close"]["summary"]
    assert (v3["n_eligible"], v3["hits"], v3["pnl_amount"]) == (168, 12, 108.96)
    s2 = after["prod|SHADOW_S2|jingcai|ah_v4_water_midpoint|macau_close"]["summary"]
    assert (s2["n_eligible"], s2["hits"], s2["pnl_amount"]) == (24, 24, -147.5)


def test_live_dbs_untouched(live_sha_before):
    for p, h in live_sha_before.items():
        assert _ledger_sig(p) == h, p


def _tmp_copy(tmp_path: Path, name: str) -> sqlite3.Connection:
    src = sqlite3.connect(f"file:{cap.DBS[name]}?mode=ro", uri=True)
    dst_p = tmp_path / f"{name}.db"
    dst = sqlite3.connect(str(dst_p))
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _snap_rat(snap: dict) -> str:
    return json.dumps([{"rule": "test"}, {"feature_snapshot": snap}], ensure_ascii=False)


def _settled_macau_matches(conn: sqlite3.Connection, n: int) -> list[int]:
    rows = conn.execute(
        """SELECT m.id FROM matches m JOIN results r ON r.match_id = m.id
           JOIN odds_asian oa ON oa.match_id = m.id AND oa.book='macau' AND oa.phase='close'
           WHERE m.scope='jingcai' AND r.home_goals IS NOT NULL ORDER BY m.id LIMIT ?""", (n,)).fetchall()
    assert len(rows) == n
    return [int(r[0]) for r in rows]


def _add_def(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    tpl = conn.execute("SELECT * FROM strategy_defs WHERE strategy_key='SHADOW_N5'").fetchone()
    cols = [c for c in tpl.keys() if c != "id"]
    vals = [tpl[c] for c in cols]
    vals[cols.index("strategy_key")] = key
    if "config_fingerprint" in cols:
        vals[cols.index("config_fingerprint")] = None
    conn.execute(f"INSERT INTO strategy_defs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    conn.commit()
    return conn.execute("SELECT * FROM strategy_defs WHERE strategy_key=?", (key,)).fetchone()


def _summary(conn, d, *, scope="jingcai", book="macau_close"):
    params = {"shadow": True, "settlement_version": bt.SETTLEMENT_VERSION}
    prep = m._prepare_strategy(conn, d, scope=scope, settle_book=book, stake_override=None, params=params)
    sim = bt.simulate(prep["bets"], rules=prep["rules"], strategy_order=[d["strategy_key"]])
    return m._single_summary(prep, sim, settle_book=book, scope=scope,
                             strategy_key=d["strategy_key"], params=params)


def test_not_evaluable_excluded_from_n_eligible(tmp_path):
    conn = _tmp_copy(tmp_path, "v2d3")
    d = _add_def(conn, "SHADOW_N5_PIN_TEST")
    mids = _settled_macau_matches(conn, 6)
    rows = [
        # 可评估 + 触发
        (mids[0], "主", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=True, edge_pin=0.041,
                                         q_model_pin=0.55, line_gap=0.0)),
        # 可评估 + 未触发（计入 n_eligible，不计 hits）
        (mids[1], "不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=True, edge_pin=0.01)),
        # 不可评估 ×4
        (mids[2], "不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                             not_evaluable_reason="pinnacle_missing",
                                             not_evaluable_subreason="stale_tick",
                                             tick_age={"pinnacle": 75.0})),
        (mids[3], "不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                             not_evaluable_reason="pinnacle_missing",
                                             not_evaluable_subreason="no_tick")),
        (mids[4], "不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                             not_evaluable_reason="model_insufficient",
                                             not_evaluable_subreason="team_n_lt_min")),
        (mids[5], "不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                             not_evaluable_reason="model_scope",
                                             not_evaluable_subreason="cup")),
    ]
    for mid, direction, snap in rows:
        se.assert_prediction_consistent(direction, snap)
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json, stake)"
                     " VALUES (?,?,?,?,1)", (mid, d["strategy_key"], direction, _snap_rat(snap)))
    conn.commit()
    s = _summary(conn, d)
    assert s["n_eligible"] == 2
    assert s["hits"] == 1
    assert s["coverage"] == 0.5
    assert s["skipped_detail"] == {"no_result": 0, "no_odds": 0, "bad_direction": 0}
    assert s["n_not_evaluable"] == 4
    assert s["n_not_evaluable_by_reason"] == {
        "pinnacle_missing": 2, "market_insufficient": 0, "model_insufficient": 1, "model_scope": 1}
    assert s["n_not_evaluable_by_subreason"] == {
        "pinnacle_missing": {"stale_tick": 1, "no_tick": 1},
        "model_insufficient": {"team_n_lt_min": 1}, "model_scope": {"cup": 1}}
    assert sorted(i["match_id"] for i in s["not_evaluable_items"]) == sorted(mids[2:])


def test_not_evaluable_does_not_shift_other_strategy(tmp_path):
    """给 V3 同场插入不可评估的别的策略预测，V3 数字不变。"""
    conn = _tmp_copy(tmp_path, "prod")
    v3 = conn.execute("SELECT * FROM strategy_defs WHERE strategy_key='CFFXDJ_5_V3'").fetchone()
    before = _summary(conn, v3)
    d = _add_def(conn, "SHADOW_N5_TEST")
    for mid in _settled_macau_matches(conn, 3):
        snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5", evaluable=False,
                                 not_evaluable_reason="market_insufficient",
                                 not_evaluable_subreason="books_lt_min", n_books=2)
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json)"
                     " VALUES (?,?,?,?)", (mid, d["strategy_key"], "不下注", _snap_rat(snap)))
    conn.commit()
    after = _summary(conn, v3)
    drop = {"data_rev", "data_rev_source"}  # data_rev 含全库 predictions 签名时会变
    assert {k: v for k, v in after.items() if k not in drop} == \
           {k: v for k, v in before.items() if k not in drop}
    s5 = _summary(conn, d)
    assert s5["n_eligible"] == 0 and s5["coverage"] is None
    assert s5["n_not_evaluable_by_reason"]["market_insufficient"] == 3


def test_not_evaluable_without_result_goes_to_skipped(tmp_path):
    """无赛果的场照旧进 skipped.no_result（与可评估场同一口径），不进 n_not_evaluable。"""
    conn = _tmp_copy(tmp_path, "v2d3")
    d = _add_def(conn, "SHADOW_N5_PIN_TEST2")
    row = conn.execute("""SELECT m.id FROM matches m LEFT JOIN results r ON r.match_id=m.id
                          WHERE (r.match_id IS NULL OR r.home_goals IS NULL) AND m.scope='jingcai'
                          LIMIT 1""").fetchone()
    if row is None:
        pytest.skip("no unsettled match in frozen copy")
    snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False, not_evaluable_reason="pinnacle_missing")
    conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json) VALUES (?,?,?,?)",
                 (int(row[0]), d["strategy_key"], "不下注", _snap_rat(snap)))
    conn.commit()
    s = _summary(conn, d)
    assert s["n_not_evaluable"] == 0
    assert s["skipped_detail"]["no_result"] == 1


def test_cached_run_without_keys_gets_zero():
    s = m._attach_ledger_metrics({"n": 5, "signal_count": 2})
    assert s["n_not_evaluable"] == 0
    assert s["n_not_evaluable_by_reason"] == {r: 0 for r in se.REASONS}
    s2 = m._attach_ledger_metrics({"n": 5, "signal_count": 2, "n_not_evaluable": 3,
                                    "n_not_evaluable_by_reason": {"pinnacle_missing": 3}})
    assert s2["n_not_evaluable"] == 3


def test_classifier_and_builder_rules():
    assert se.not_evaluable_of(_snap_rat({"evaluable": True})) is None
    assert se.not_evaluable_of(_snap_rat({"filter_hit": True})) is None
    assert se.not_evaluable_of(None) is None
    assert se.not_evaluable_of("not json") is None
    assert se.not_evaluable_of(_snap_rat({"evaluable": False})) == ("unspecified", None)
    assert se.not_evaluable_of({"not_evaluable_reason": "model_scope"}) == ("model_scope", None)
    with pytest.raises(ValueError):  # market_insufficient 只用于 N5
        se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False, not_evaluable_reason="market_insufficient")
    with pytest.raises(ValueError):  # pinnacle_missing 只用于 N5-PIN
        se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5", evaluable=False, not_evaluable_reason="pinnacle_missing")
    with pytest.raises(ValueError):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False, not_evaluable_reason="pinnacle_missing",
                          not_evaluable_subreason="bogus")
    with pytest.raises(ValueError):
        se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=True, edge_median=0.1)
    with pytest.raises(ValueError):  # tick 晚于目标时点 = 泄漏
        se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5", evaluable=True, tick_age={"macau": -1})
    with pytest.raises(ValueError):
        se.assert_prediction_consistent("主", {"evaluable": False, "not_evaluable_reason": "model_scope"})
    snap = se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5", evaluable=True, n_books=4,
                             tick_age={"macau": 3, "crown": 12.5}, edge_median=0.035)
    for k in ("n_books", "tick_age", "line_gap", "q_model_pin", "q_model_macau", "edge_pin",
              "edge_macau", "edge_median", "evaluable", "not_evaluable_reason"):
        assert k in snap


# ---------------- /strategies/{id}/validate HTTP 层 ----------------

def test_validate_endpoint_returns_not_evaluable_fields(tmp_path, monkeypatch):
    import app.db as adb
    from fastapi.testclient import TestClient
    conn = _tmp_copy(tmp_path, "v2d3")
    d = _add_def(conn, "SHADOW_N5_PIN_HTTP")
    mids = _settled_macau_matches(conn, 3)
    snaps = [("主", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=True, edge_pin=0.05)),
             ("不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                         not_evaluable_reason="pinnacle_missing",
                                         not_evaluable_subreason="stale_tick")),
             ("不下注", se.build_snapshot(**N5RHO, fingerprint=N5FP, odds_source="live", ledger_id="N5-PIN", evaluable=False,
                                         not_evaluable_reason="model_insufficient"))]
    for mid, (dr, sn) in zip(mids, snaps):
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json) VALUES (?,?,?,?)",
                     (mid, d["strategy_key"], dr, _snap_rat(sn)))
    conn.commit()
    conn.close()
    monkeypatch.setattr(adb, "DB_PATH", tmp_path / "v2d3.db")
    from app.main import app as fastapi_app
    r = TestClient(fastapi_app).post(f"/strategies/{d['id']}/validate", json={})
    assert r.status_code == 202, r.text
    s = r.json()["summary"]
    assert (s["n_eligible"], s["hits"], s["n_not_evaluable"]) == (1, 1, 2)
    assert s["n_not_evaluable_by_reason"]["pinnacle_missing"] == 1
    assert s["n_not_evaluable_by_reason"]["model_insufficient"] == 1


# ---------------- seed_shadow_strategy_defs 保护（只在临时拷贝 dry-run） ----------------

def test_seed_guard_skips_n5_family(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_shadow_strategy_defs as seed
    conn = _tmp_copy(tmp_path, "v2d3")
    pending, _ = seed.load_pending_rows(seed.CSV_PATH)
    ids = {r["id"] for r in pending}
    if not ids:
        pytest.skip("影子方案台账只有表头：公开仓不含真实台账（research/shadow-ledger/shadow-schemes.csv）")
    assert "N5-PIN" in ids  # CSV 已有 N5-PIN 的 pending_shadow 行——保护必须拦住
    reasons = {r["id"]: seed.guard_reason(conn, r) for r in pending}
    assert reasons["N5-PIN"].startswith("blocked_until_pipeline")
    assert reasons["N5"].startswith("blocked_until_pipeline")
    assert seed.BUCKET_BY_ID["N5-PIN"] == "G"
    # 任何能被种子的行都必须有 bucket
    for r in pending:
        if reasons[r["id"]] is None:
            assert seed.BUCKET_BY_ID.get(r["id"]) is not None
    before = conn.execute("SELECT COUNT(*) FROM strategy_defs").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM strategy_defs WHERE strategy_key='SHADOW_N5_PIN'").fetchone()[0] == 0
    assert before == conn.execute("SELECT COUNT(*) FROM strategy_defs").fetchone()[0]



# ---------------- 来源分账 odds_source（接入细则 5/6） ----------------

def test_builder_requires_odds_source():
    for bad in (None, "", "LIVE ", "backfill", "unknown"):
        with pytest.raises(ValueError):
            se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5-PIN", evaluable=True, odds_source=bad)
    with pytest.raises(ValueError):  # 省略也不行
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True)
    with pytest.raises(ValueError):  # hist 不按 tick 时效剔除
        se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5-PIN", evaluable=False, odds_source="hist",
                          not_evaluable_reason="pinnacle_missing", not_evaluable_subreason="stale_tick")
    ok = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5-PIN", evaluable=False, odds_source="hist",
                           not_evaluable_reason="pinnacle_missing", not_evaluable_subreason="no_tick",
                           tick_age={"pinnacle": 300.0})
    assert ok["odds_source"] == "hist" and ok["tick_age_rule"] == "hist_in_effect"
    live = se.build_snapshot(**N5RHO, fingerprint=N5FP, ledger_id="N5", evaluable=True, odds_source="live")
    assert live["tick_age_rule"] == "live_fetched_at"
    assert se.odds_source_of(live) == "live"
    assert se.odds_source_of({"odds_source": "weird"}) == "unknown"
    assert se.odds_source_of({}) == "unknown"
    assert se.odds_source_of(_snap_rat({"odds_source": "hist"})) == "hist"


def test_validate_split_by_odds_source(tmp_path):
    conn = _tmp_copy(tmp_path, "v2d3")
    d = _add_def(conn, "SHADOW_N5_PIN_SRC")
    mids = _settled_macau_matches(conn, 7)
    B = _b5
    rows = [
        (mids[0], "主", B(ledger_id="N5-PIN", odds_source="live", evaluable=True, edge_pin=0.05)),
        (mids[1], "不下注", B(ledger_id="N5-PIN", odds_source="live", evaluable=True, edge_pin=0.0)),
        (mids[2], "不下注", B(ledger_id="N5-PIN", odds_source="live", evaluable=False,
                             not_evaluable_reason="pinnacle_missing", not_evaluable_subreason="stale_tick")),
        (mids[3], "客", B(ledger_id="N5-PIN", odds_source="hist", evaluable=True, edge_pin=-0.04)),
        (mids[4], "主", B(ledger_id="N5-PIN", odds_source="hist", evaluable=True, edge_pin=0.04)),
        (mids[5], "不下注", B(ledger_id="N5-PIN", odds_source="hist", evaluable=False,
                             not_evaluable_reason="model_scope", not_evaluable_subreason="cup")),
        (mids[6], "主", {"edge_pin": 0.06}),  # 无 odds_source → unknown
    ]
    for mid, dr, sn in rows:
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json, stake)"
                     " VALUES (?,?,?,?,1)", (mid, d["strategy_key"], dr, _snap_rat(sn)))
    conn.commit()
    s = _summary(conn, d)
    # 顶层：全部合计（向后兼容）
    assert (s["n_eligible"], s["hits"], s["n_not_evaluable"]) == (5, 4, 2)
    assert s["odds_source_split"] is True
    live, hist, unk = (s["by_odds_source"][k] for k in ("live", "hist", "unknown"))
    assert (live["n_eligible"], live["hits"], live["coverage"]) == (2, 1, 0.5)
    assert live["n_not_evaluable"] == 1
    assert live["n_not_evaluable_by_reason"]["pinnacle_missing"] == 1
    assert live["n_not_evaluable_by_subreason"] == {"pinnacle_missing": {"stale_tick": 1}}
    assert (hist["n_eligible"], hist["hits"], hist["n_not_evaluable"]) == (2, 2, 1)
    assert hist["n_not_evaluable_by_reason"]["model_scope"] == 1
    assert (unk["n_eligible"], unk["hits"], unk["n_not_evaluable"]) == (1, 1, 0)
    # hits 不跨来源合并：各桶相加 = 顶层
    assert live["hits"] + hist["hits"] + unk["hits"] == s["hits"]
    assert live["n_eligible"] + hist["n_eligible"] + unk["n_eligible"] == s["n_eligible"]
    for sub in (live, hist, unk):
        for key in ("roi", "pnl_amount", "stake_total_amount", "max_drawdown", "final_bankroll",
                    "bet_count", "result_counts", "n_not_evaluable_by_reason"):
            assert key in sub
        assert sub["initial_bankroll"] == s["initial_bankroll"]  # 各自资金曲线从同一本金起
    assert {i["odds_source"] for i in s["not_evaluable_items"]} == {"live", "hist"}


def test_cached_run_backfill_by_odds_source():
    top = {"n": 3, "sample_count": 3, "signal_count": 2, "n_eligible": 3, "hits": 2,
           "coverage": 2 / 3, "roi": 0.1, "pnl_amount": 10.0, "stake_total_amount": 100.0,
           "initial_bankroll": 10000.0, "final_bankroll": 10010.0, "bet_count": 2}
    s = m._attach_ledger_metrics(dict(top))
    assert s["odds_source_split"] is True and s["odds_source_split_backfilled"] is True
    unk = s["by_odds_source"]["unknown"]
    for k, v in top.items():
        assert unk[k] == v
    assert s["by_odds_source"]["live"]["n_eligible"] == 0
    assert s["by_odds_source"]["hist"]["coverage"] is None
    assert s["open_basis_split"] is True and s["open_basis_split_backfilled"] is True
    for k, v in top.items():
        assert s["by_open_basis"]["unknown"][k] == v
    assert s["by_open_basis"]["api_opening"]["n_eligible"] == 0
    # 已有 by_odds_source 的 summary 不被改写
    s2 = m._attach_ledger_metrics(dict(s))
    assert s2["by_odds_source"] is s["by_odds_source"]


def test_compare_stack_split(tmp_path, monkeypatch):
    import app.db as adb
    from fastapi.testclient import TestClient
    conn = _tmp_copy(tmp_path, "v2d3")
    d1 = _add_def(conn, "SHADOW_N5_PIN_CMP")
    d2 = _add_def(conn, "SHADOW_N5_CMP")
    mids = _settled_macau_matches(conn, 3)
    B = _b5
    preds = [
        (mids[0], d1, "主", B(ledger_id="N5-PIN", odds_source="live", evaluable=True, edge_pin=0.05)),
        (mids[1], d1, "不下注", B(ledger_id="N5-PIN", odds_source="hist", evaluable=False,
                                 not_evaluable_reason="pinnacle_missing", not_evaluable_subreason="no_tick")),
        (mids[0], d2, "主", B(ledger_id="N5", odds_source="live", evaluable=True, edge_median=0.04)),
        (mids[2], d2, "客", B(ledger_id="N5", odds_source="hist", evaluable=True, edge_median=-0.05)),
    ]
    for mid, d, dr, sn in preds:
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json, stake)"
                     " VALUES (?,?,?,?,1)", (mid, d["strategy_key"], dr, _snap_rat(sn)))
    conn.commit()
    ids = [d1["id"], d2["id"]]
    conn.close()
    monkeypatch.setattr(adb, "DB_PATH", tmp_path / "v2d3.db")
    from app.main import app as fastapi_app
    r = TestClient(fastapi_app).post("/strategies/compare", json={"strategy_ids": ids})
    assert r.status_code == 200, r.text
    j = r.json()
    for it in j["items"]:
        assert it["summary"]["odds_source_split"] is True
    st = j["stack"]["summary"]
    assert st["odds_source_split"] is True
    assert st["by_odds_source"]["live"]["hits"] == 2
    assert st["by_odds_source"]["hist"]["hits"] == 1
    assert st["by_odds_source"]["hist"]["n_not_evaluable"] == 1
    ps = st["per_strategy"]
    assert ps["SHADOW_N5_PIN_CMP"]["by_odds_source"]["live"]["hits"] == 1
    assert ps["SHADOW_N5_PIN_CMP"]["by_odds_source"]["hist"]["n_not_evaluable"] == 1
    assert ps["SHADOW_N5_CMP"]["by_odds_source"]["hist"]["hits"] == 1
    assert ps["SHADOW_N5_CMP"]["by_odds_source"]["unknown"]["n_eligible"] == 0
    # open_basis 未给 → 全部 unknown；stack 与 per_strategy 都有该分账
    assert st["open_basis_split"] is True
    assert st["by_open_basis"]["unknown"]["hits"] == st["hits"]
    assert st["by_open_basis"]["first_tick"]["n_eligible"] == 0
    assert ps["SHADOW_N5_PIN_CMP"]["by_open_basis"]["unknown"]["hits"] == 1



# ---------------- 初盘口径 open_basis ----------------

def test_builder_open_basis_rules():
    B = _b5
    base = dict(ledger_id="N5-PIN", evaluable=True, odds_source="live")
    # 可选：不给 → 快照不写 open_basis → validate 归 unknown
    s0 = B(**base)
    assert "open_basis" not in s0 and se.open_basis_of(s0) == "unknown"
    assert s0["decision_phase"] == "close"
    for ok in ("first_tick", "api_opening"):
        assert B(**base, open_basis=ok)["open_basis"] == ok
    for bad in ("", "opening", "API_OPENING", "unknown", "first"):
        with pytest.raises(ValueError):
            B(**base, open_basis=bad)
    with pytest.raises(ValueError):  # 特征名不认识
        B(**base, open_basis="first_tick", open_features_used=["open_foo"])
    with pytest.raises(ValueError):  # 用了初盘特征却没标口径
        B(**base, open_features_used=["open_line"])
    with pytest.raises(ValueError):
        B(**base, decision_phase="live")
    # first_tick 不需要 usable 标记
    s1 = B(**base, open_basis="first_tick", open_features_used=["open_line", "open_kelly"])
    assert s1["open_features_used"] == ["open_kelly", "open_line"]


def test_builder_api_opening_requires_usable_flag():
    B = _b5
    base = dict(ledger_id="N5", evaluable=True, odds_source="hist", open_basis="api_opening")
    # 没用初盘派生特征 → 只记录口径，不要求标记
    assert B(**base)["open_basis"] == "api_opening"
    for feat in sorted(se.OPEN_DERIVED_FEATURES):
        # close 决策：usable_at_close 必须为 True
        for flag in (None, False):
            with pytest.raises(ValueError):
                B(**base, open_features_used=[feat], usable_at_close=flag)
        with pytest.raises(ValueError):  # 只有 mid 可用不够
            B(**base, open_features_used=[feat], usable_at_mid=True, usable_at_close=False)
        ok = B(**base, open_features_used=[feat], usable_at_close=True)
        assert ok["usable_at_close"] is True
        # mid 决策：看 usable_at_mid
        with pytest.raises(ValueError):
            B(**base, open_features_used=[feat], decision_phase="mid", usable_at_close=True)
        assert B(**base, open_features_used=[feat], decision_phase="mid",
                 usable_at_mid=True)["decision_phase"] == "mid"
    with pytest.raises(ValueError):  # 标记必须是 bool
        B(**base, open_features_used=["open_line"], usable_at_close="yes")


def test_validate_split_by_open_basis(tmp_path):
    conn = _tmp_copy(tmp_path, "v2d3")
    d = _add_def(conn, "SHADOW_N5_PIN_OB")
    mids = _settled_macau_matches(conn, 5)
    B = _b5
    rows = [
        (mids[0], "主", B(ledger_id="N5-PIN", odds_source="live", evaluable=True, open_basis="first_tick")),
        (mids[1], "客", B(ledger_id="N5-PIN", odds_source="live", evaluable=True, open_basis="api_opening",
                         open_features_used=["open_line"], usable_at_close=True)),
        (mids[2], "不下注", B(ledger_id="N5-PIN", odds_source="hist", evaluable=True, open_basis="api_opening")),
        (mids[3], "不下注", B(ledger_id="N5-PIN", odds_source="hist", evaluable=False, open_basis="first_tick",
                             not_evaluable_reason="pinnacle_missing", not_evaluable_subreason="no_tick")),
        (mids[4], "主", B(ledger_id="N5-PIN", odds_source="live", evaluable=True)),  # 无 open_basis
    ]
    for mid, dr, sn in rows:
        conn.execute("INSERT INTO predictions (match_id, strategy, direction, rationale_json, stake)"
                     " VALUES (?,?,?,?,1)", (mid, d["strategy_key"], dr, _snap_rat(sn)))
    conn.commit()
    s = _summary(conn, d)
    assert (s["n_eligible"], s["hits"], s["n_not_evaluable"]) == (4, 3, 1)
    assert s["open_basis_split"] is True and s["odds_source_split"] is True
    ft, ao, unk = (s["by_open_basis"][k] for k in ("first_tick", "api_opening", "unknown"))
    assert (ft["n_eligible"], ft["hits"], ft["n_not_evaluable"]) == (1, 1, 1)
    assert ft["n_not_evaluable_by_reason"]["pinnacle_missing"] == 1
    assert (ao["n_eligible"], ao["hits"], ao["coverage"]) == (2, 1, 0.5)
    assert (unk["n_eligible"], unk["hits"]) == (1, 1)
    assert ft["hits"] + ao["hits"] + unk["hits"] == s["hits"]
    # 两个维度互相独立
    assert s["by_odds_source"]["live"]["hits"] == 3
    assert {i["open_basis"] for i in s["not_evaluable_items"]} == {"first_tick"}
