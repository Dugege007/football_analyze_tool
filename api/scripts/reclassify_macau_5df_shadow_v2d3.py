#!/usr/bin/env python3
"""0.3.22：v2d3 影子元数据重分类（只写副本）。

把曾按 ah.macau 真实水位（实为 5DF）生成的 SHADOW_S1 / SHADOW_S1_V2
标记 water_src_reclassified=macau_to_5df，feature_book 旁注 macau_5df。
不改 direction / settle_book / 注单结果。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2D3 = ROOT / "data" / "v2d3" / "app.db"
LIVE = ROOT / "data" / "app.db"
TZ = timezone(timedelta(hours=8))
KEYS = ("SHADOW_S1", "SHADOW_S1_V2")
FLAG = "macau_to_5df"


def _touch_fs(fs: dict) -> bool:
    changed = False
    if fs.get("water_src_reclassified") != FLAG:
        fs["water_src_reclassified"] = FLAG
        changed = True
    if fs.get("feature_book") == "macau" or fs.get("feature_water_book") == "macau":
        # 旁注：原 feature_book 保留，加 lane
        if fs.get("book_lane") != "macau_5df":
            fs["book_lane"] = "macau_5df"
            changed = True
        if fs.get("feature_book_lane") != "macau_5df":
            fs["feature_book_lane"] = "macau_5df"
            changed = True
    src = fs.get("source") or ""
    if "5df" in str(src) or fs.get("water_src") == "actual":
        if fs.get("water_src_reclassified") != FLAG:
            fs["water_src_reclassified"] = FLAG
            changed = True
    return changed


def reclass_rationale(raw: str | None) -> tuple[str | None, bool]:
    if not raw:
        return raw, False
    try:
        arr = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    if not isinstance(arr, list):
        return raw, False
    changed = False
    for i, x in enumerate(arr):
        if isinstance(x, dict) and isinstance(x.get("feature_snapshot"), dict):
            if _touch_fs(x["feature_snapshot"]):
                changed = True
            # also top-level flag on the wrapper dict
            if x.get("water_src_reclassified") != FLAG:
                x["water_src_reclassified"] = FLAG
                changed = True
    note = f"water_src_reclassified={FLAG} (0.3.22 ah.macau_5df lane)"
    if changed and note not in arr:
        arr.append(note)
    return (json.dumps(arr, ensure_ascii=False) if changed else raw), changed


def reclass_strategy(conn: sqlite3.Connection, key: str, dry: bool) -> dict:
    row = conn.execute(
        "SELECT id, notes, config_json FROM strategy_defs WHERE strategy_key=?", (key,)
    ).fetchone()
    if not row:
        return {"key": key, "strategy_defs": "missing"}
    cfg = json.loads(row["config_json"] or "{}")
    ex = cfg.setdefault("extras", {})
    changed = False
    if ex.get("water_src_reclassified") != FLAG:
        ex["water_src_reclassified"] = FLAG
        changed = True
    if ex.get("book_lane") != "macau_5df":
        ex["book_lane"] = "macau_5df"
        changed = True
    if ex.get("feature_book_lane") != "macau_5df":
        ex["feature_book_lane"] = "macau_5df"
        changed = True
    # keep feature_book=macau for hist identity but note parallel column
    ex.setdefault("feature_book_note", "0.3.22: 水位特征对应 ah.macau_5df（原写入 odds_asian.macau 的 5DF 水）")
    notes = row["notes"] or ""
    tag = "[0.3.22 water_src_reclassified=macau_to_5df → ah.macau_5df]"
    if tag not in notes:
        notes = (notes + " " + tag).strip()
        changed = True
    n_pred = 0
    for pr in conn.execute(
        "SELECT id, rationale_json FROM predictions WHERE strategy=?", (key,)
    ):
        new_r, ch = reclass_rationale(pr["rationale_json"])
        if ch:
            n_pred += 1
            if not dry:
                conn.execute(
                    "UPDATE predictions SET rationale_json=? WHERE id=?",
                    (new_r, pr["id"]),
                )
    if changed and not dry:
        conn.execute(
            "UPDATE strategy_defs SET config_json=?, notes=?, updated_at=? WHERE id=?",
            (json.dumps(cfg, ensure_ascii=False), notes,
             datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
        )
    return {"key": key, "strategy_defs_changed": changed, "predictions_flagged": n_pred}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=V2D3)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = args.db.resolve()
    if db.resolve() == LIVE.resolve():
        raise SystemExit("REFUSE: will not write live data/app.db")
    if "v2d3" not in str(db):
        raise SystemExit(f"REFUSE: expected v2d3 path, got {db}")
    dry = not args.apply
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = {"db": str(db), "dry_run": dry, "at": datetime.now(TZ).isoformat(), "items": []}
    try:
        for k in KEYS:
            out["items"].append(reclass_strategy(conn, k, dry))
        if not dry:
            conn.commit()
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
