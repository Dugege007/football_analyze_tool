"""Capture validate summaries (no DB writes) for regression: S1/S2/S8/N1/N4/V3 + mirror.

用法：python tests/regress_validate_capture.py <out.json>
读 tests/fixtures/regress_db/{prod,v2d3}.db（冻结只读副本），只调用 _prepare_strategy + simulate +
_single_summary + _cache_rows_from_entries，不写 strategy_validation_runs。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backtest as bt  # noqa: E402
from app import main as m  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "regress_db"
DBS = {"prod": FIX / "prod.db", "v2d3": FIX / "v2d3.db"}
KEYS = ["SHADOW_S1", "SHADOW_S2", "SHADOW_S8", "SHADOW_N1", "SHADOW_N4",
        "CFFXDJ_5_V3", "TEST_V3_MIRROR"]
SCOPES = ["jingcai", "all"]
VERSIONS = [bt.SETTLEMENT_VERSION, bt.SETTLEMENT_MACAU_ACTUAL]
BOOKS = ["macau_close", "crown_close"]


def capture() -> dict:
    out: dict = {}
    for dbname, path in DBS.items():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for key in KEYS:
            d = conn.execute(
                "SELECT * FROM strategy_defs WHERE strategy_key = ? ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            if not d:
                continue
            for scope in SCOPES:
                for ver in VERSIONS:
                    for book in BOOKS:
                        params = {"shadow": True, "settlement_version": ver}
                        prep = m._prepare_strategy(conn, d, scope=scope, settle_book=book,
                                                   stake_override=None, params=params)
                        sim = bt.simulate(prep["bets"], rules=prep["rules"], strategy_order=[key])
                        s = m._single_summary(prep, sim, settle_book=book, scope=scope,
                                              strategy_key=key, params=params)
                        rows = m._cache_rows_from_entries(sim["entries"], book, settlement_version=ver)
                        out[f"{dbname}|{key}|{scope}|{ver}|{book}"] = {
                            "summary": s,
                            "row_fingerprints": [r["row_fingerprint"] for r in rows],
                        }
        conn.close()
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    data = capture()
    Path(sys.argv[1]).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    print(len(data), "cases")
