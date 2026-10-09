#!/usr/bin/env python3
"""可选：给 strategy_defs 加 leak_suspect 列并按 config/leak_suspect.json 回填（0.3.20 准备，未执行）。

⚠ 现网 data/app.db 写库需要维护者明确批准。API 0.3.20 不依赖这一列：leak_suspect 由 config/leak_suspect.json 叠加，
现网 / v2d3 行为一致。只有以后想在 SQL 里直接查这个字段时才需要跑。注意：LEGACY V4–V7 目前不在任何库的
strategy_defs 里，所以就算跑了，回填行数也是 0（只是先把列建好）。

用法：
  干跑（默认，只读）：    .venv/bin/python scripts/migrate_leak_suspect_live.py [--db data/app.db]
  执行（需批准）：        ... --apply --approved-by "<审批人> 2026-10-xx hh:mm"
  回滚（删列，先备份）：  ... --rollback --approved-by "..."
执行 / 回滚前一律用 sqlite backup API 整库备份到 backups/leak-suspect-<ts>/app.db，并写 manifest.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import leak_suspect as leak  # noqa: E402


def _cols(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(strategy_defs)")]


def _backup(db: Path, tag: str) -> Path:
    d = ROOT / "backups" / f"leak-suspect-{tag}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    d.mkdir(parents=True, exist_ok=False)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(d / "app.db"))
    src.backup(dst)
    src.close()
    dst.close()
    return d


def plan(conn: sqlite3.Connection) -> dict:
    data = leak.load()
    keys = {r[0] for r in conn.execute("SELECT DISTINCT strategy_key FROM strategy_defs")}
    rows = []
    for e in data.get("entries") or []:
        names = [e["strategy_key"], *(e.get("aliases") or [])]
        hit = sorted(set(names) & keys)
        rows.append({"strategy_key": e["strategy_key"], "leak_suspect": e["leak_suspect"], "in_strategy_defs": hit})
    n = sum(conn.execute("SELECT COUNT(*) FROM strategy_defs WHERE strategy_key = ?", (k,)).fetchone()[0]
            for r in rows for k in r["in_strategy_defs"])
    return {"column_exists": "leak_suspect" in _cols(conn), "entries": rows, "rows_to_update": n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "app.db"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--approved-by", default=None, help="批准人 + 时间（写库必填）")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if os.environ.get("DUAL_WRITE", "0").lower() in ("1", "true", "on", "yes"):
        print("refuse: DUAL_WRITE is on (must stay off)")
        return 2
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    p = plan(ro)
    ro.close()
    print(json.dumps({"db": str(db), "mode": "apply" if a.apply else "rollback" if a.rollback else "dry_run", **p},
                     ensure_ascii=False, indent=1))
    if not (a.apply or a.rollback):
        return 0
    if not a.approved_by:
        print("refuse: --approved-by required for any write (live DB needs owner approval)")
        return 2
    bak = _backup(db, "rollback" if a.rollback else "apply")
    conn = sqlite3.connect(str(db))
    try:
        if a.apply:
            if not p["column_exists"]:
                conn.execute("ALTER TABLE strategy_defs ADD COLUMN leak_suspect TEXT")
            n = 0
            for r in p["entries"]:
                for k in r["in_strategy_defs"]:
                    n += conn.execute("UPDATE strategy_defs SET leak_suspect = ? WHERE strategy_key = ?",
                                      (r["leak_suspect"], k)).rowcount
            done = {"added_column": not p["column_exists"], "rows_updated": n}
        else:
            if p["column_exists"]:
                conn.execute("ALTER TABLE strategy_defs DROP COLUMN leak_suspect")
            done = {"dropped_column": p["column_exists"]}
        conn.commit()
    finally:
        conn.close()
    man = {"db": str(db), "backup": str(bak / "app.db"), "approved_by": a.approved_by,
           "at": datetime.now().astimezone().isoformat(timespec="seconds"), "plan": p, "done": done,
           "restore": f"cp {bak / 'app.db'} {db}  # 停服务后整库还原"}
    (bak / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(man, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
