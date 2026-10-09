"""建/重建测试用影子方案 TEST_V3_MIRROR（幂等，可重复执行）。

- strategy_defs：status=shadow，config.extras.test_only=true（仅用于测试冲突表，不进日用白名单）。
- predictions（strategy=TEST_V3_MIRROR，只读 V3 生成，不改 V3 行）：
  1) V3 有方向（主/客）的场 → 取反向；
  2) V3「不下注」且 match_id % 4 == 0 的场 → (match_id // 4) 奇数=主、偶数=客（确定性规则）。
- 现网隔离：/matches 的 has_prediction 计数排除 extras.test_only 方案；
  /prediction、/predictions、/bankroll/calc 默认只取 CFFXDJ_5_V3；dispatch 不读 predictions。

用法：.venv/bin/python scripts/seed_test_v3_mirror.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect  # noqa: E402
from app.main import config_fingerprint, normalize_strategy_config  # noqa: E402

KEY = "TEST_V3_MIRROR"
VERSION = "2026.10.06-test"
SRC = "CFFXDJ_5_V3"
NOTE = "仅用于测试冲突表，不进日用白名单"


def main() -> None:
    conn = connect()
    cfg = normalize_strategy_config({
        "settle_book": "macau_close",
        "juice": 0.95,
        "extras": {"test_only": True, "test_note": NOTE, "mirror_of": SRC,
                   "rule": "V3 主/客取反；V3 不下注且 id%4==0 → (id//4) 奇主偶客"},
    })
    row = conn.execute("SELECT id FROM strategy_defs WHERE strategy_key=? AND version=?",
                       (KEY, VERSION)).fetchone()
    if row:
        sid = row["id"]
        conn.execute(
            "UPDATE strategy_defs SET config_json=?, config_fingerprint=?, status='shadow', is_default=0,"
            " notes=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(cfg, ensure_ascii=False), config_fingerprint(cfg), NOTE, sid))
    else:
        cur = conn.execute(
            "INSERT INTO strategy_defs (strategy_key, version, display_name, markets_json, config_json,"
            " config_fingerprint, status, is_default, notes) VALUES (?,?,?,?,?,?, 'shadow', 0, ?)",
            (KEY, VERSION, "V3 镜像（测试）", '["ah"]', json.dumps(cfg, ensure_ascii=False),
             config_fingerprint(cfg), NOTE))
        sid = cur.lastrowid

    conn.execute("DELETE FROM predictions WHERE strategy=?", (KEY,))
    n = 0
    for r in conn.execute("SELECT match_id, direction FROM predictions WHERE strategy=? ORDER BY match_id",
                          (SRC,)).fetchall():
        mid, d = int(r["match_id"]), (r["direction"] or "").strip()
        if d == "主":
            nd = "客"
        elif d == "客":
            nd = "主"
        elif mid % 4 == 0:
            nd = "主" if (mid // 4) % 2 == 1 else "客"
        else:
            continue
        conn.execute(
            "INSERT INTO predictions (match_id, strategy, direction, settle_book, rationale_json, stake_rule)"
            " VALUES (?,?,?,?,?,?)",
            (mid, KEY, nd, "macau_close",
             json.dumps([f"mirror_of={SRC}", f"src_direction={d}", "test_only"], ensure_ascii=False),
             "test_only"))
        n += 1
    conn.commit()
    print(json.dumps({"strategy_def_id": sid, "strategy_key": KEY, "predictions": n}, ensure_ascii=False))


if __name__ == "__main__":
    main()
