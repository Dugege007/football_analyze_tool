#!/usr/bin/env python3
"""从影子台账 CSV 幂等种子 strategy_defs（status=shadow，test_only，无 predictions）。

- 只处理 status=pending_shadow；跳过 blocked_need_*（如 N2）
- 保护（2026-10-08，N5/N5-PIN 口径卡 v1 §7）：
  * BLOCKED_UNTIL_PIPELINE 里的 ledger（N5、N5-PIN）即使 CSV 是 pending_shadow 也跳过——
    前向平博采集 + Dixon-Coles 管线接通前不建/不覆盖定义（N5 现有 def 不被改写）；
    挂载时走 n5pin 笔记 §6 的专门步骤（新 version），不靠本脚本。
  * BUCKET_BY_ID 无映射的 ledger 跳过（不建 bucket=null 的空定义）。
  * 同 strategy_key 已有其它 version 的定义时跳过（防止插入一条更旧的 2026.10.06-shadow 平行版本）。
  * 跳过原因进输出 skipped_guard。
- strategy_key = SHADOW_<id>（非字母数字 → _）
- 可重复执行：同 strategy_key+version → UPDATE；否则 INSERT
- 不写 predictions；不改 CFFXDJ_5_V3 / TEST_V3_MIRROR / odds_asian

用法：
  .venv/bin/python scripts/seed_shadow_strategy_defs.py
  .venv/bin/python scripts/seed_shadow_strategy_defs.py --db data/app.db --dry-run
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


import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.main import config_fingerprint, normalize_strategy_config  # noqa: E402

CSV_PATH = Path(str(_REPO_ROOT / "research/shadow-ledger/shadow-schemes.csv"))
VERSION = "2026.10.06-shadow"
DISPLAY_PREFIX = "影子·"

# mutex-buckets-min-n.md §2
BUCKET_BY_ID: dict[str, str] = {
    "N4": "A",
    "S2": "B",
    "S1": "C",
    "N1": "D",
    "N3": "E",
    "S8": "F",
    "N5": "G",
    "N5-PIN": "G",  # 与 N5 同族分账（不合并命中）
    "IP-B": "H",
    "IP-A": "H",
    "XG-BSD-1": "I",
}
MUTEX_NOTE: dict[str, str] = {
    "S1": "桶C 三段不动；与 B 几乎正交；同场命中 A/B 标 multi_hit、PnL 分账",
    "S2": "桶B 动态升盘；两支路分账；与 N4 联测禁止合并 PnL",
    "S8": "桶F 过滤层；评估=滤后 V3 vs 未滤 V3；不单独选边",
    "N1": "桶D 顺分布排除；与 A/B/C 不同维可并行",
    "N3": "桶E 单拉平阻；与 N1 同用 1X2 时双命中→multi_hit 分账",
    "N4": "桶A 静态深浅×高水；与 S2 分账",
    "N5": "桶G 泊松偏差；必报 CLV；与盘口叙事族分账",
    "N5-PIN": "桶G 与 N5 同族分账、命中不合并；市场概率只用平博；11-06 与 N5 逐场配对",
    "IP-B": "桶H 滚球 stub 底座；未完成前 IP-A 不评 ROI",
    "IP-A": "桶H 依赖 IP-B；竞彩∩清醒窗",
    "XG-BSD-1": "桶I xG stub；覆盖缺口另记，不与 A–E 合并",
}


# 管线未接通前不得由种子脚本建/改的 ledger（口径卡 v2_0-n5-poisson-spec-v1.md §7）
BLOCKED_UNTIL_PIPELINE: dict[str, str] = {
    "N5": "blocked_until_pipeline: 前向平博采集+Dixon-Coles 未接通（口径卡 §7），现有 def 不覆盖",
    "N5-PIN": "blocked_until_pipeline: 前向平博采集+Dixon-Coles 未接通（口径卡 §7）",
}


def guard_reason(conn, row: dict[str, str]) -> str | None:
    """返回跳过原因；None = 可种子。"""
    lid = (row.get("id") or "").strip()
    if lid in BLOCKED_UNTIL_PIPELINE:
        return BLOCKED_UNTIL_PIPELINE[lid]
    if BUCKET_BY_ID.get(lid) is None:
        return "no_bucket_mapping: BUCKET_BY_ID 无此 ledger，拒绝建 bucket=null 的空定义"
    key = strategy_key_for(lid)
    other = conn.execute(
        "SELECT version FROM strategy_defs WHERE strategy_key=? AND version<>? ORDER BY version DESC LIMIT 1",
        (key, VERSION),
    ).fetchone()
    if other is not None:
        return f"other_version_exists: {key}@{other['version']}（不插平行旧版本）"
    return None


def strategy_key_for(ledger_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ledger_id).strip("_").upper()
    return f"SHADOW_{safe}"


def is_stub(notes: str) -> bool:
    return "stub" in (notes or "").lower()


def build_config(row: dict[str, str]) -> dict:
    lid = row["id"]
    stub = is_stub(row.get("notes") or "")
    extras = {
        "test_only": True,
        "test_note": "仅影子对照，不进日用白名单",
        "ledger_id": lid,
        "rule_one_liner": row.get("rule_one_liner") or "",
        "data_deps": row.get("data_deps") or "",
        "bucket": BUCKET_BY_ID.get(lid),
        "report_date": row.get("report_date") or "",
        "source": row.get("source") or "",
        "notes": row.get("notes") or "",
        "mutex_note": MUTEX_NOTE.get(lid) or "",
        "settle_default_ledger": row.get("settle_default") or "",
        "shadow_stub": stub,
        "predictions": "none_until_rules_finalized",
        "validate_note": (
            "无 predictions 时 validate 的 n_eligible/hits=0、coverage=null；"
            "待规则卡与特征齐后再 ingest"
        ),
    }
    return normalize_strategy_config(
        {
            "settle_book": "macau_close",
            "juice": 0.95,
            "vote": {"members": [], "threshold": 3},
            "gates": [],
            "stake_rule": "fractional_quarter_kelly_units",
            "state_machine": None,
            "feature_refs": [],
            "extras": extras,
        }
    )


def load_pending_rows(path: Path) -> tuple[list[dict], list[dict]]:
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pending, skipped = [], []
    for r in rows:
        st = (r.get("status") or "").strip()
        if st == "pending_shadow":
            pending.append(r)
        else:
            skipped.append(r)
    return pending, skipped


def upsert(conn, row: dict[str, str], *, dry_run: bool) -> dict:
    key = strategy_key_for(row["id"])
    cfg = build_config(row)
    fp = config_fingerprint(cfg)
    display = f"{DISPLAY_PREFIX}{row['id']}"
    notes = (
        f"影子台账 {row['id']} · bucket={BUCKET_BY_ID.get(row['id'])} · "
        f"{'stub · ' if is_stub(row.get('notes') or '') else ''}"
        f"仅对照不进日用；无 predictions"
    )
    existing = conn.execute(
        "SELECT id FROM strategy_defs WHERE strategy_key=? AND version=?",
        (key, VERSION),
    ).fetchone()
    action = "update" if existing else "insert"
    sid = int(existing["id"]) if existing else None
    if dry_run:
        return {
            "action": action,
            "strategy_key": key,
            "id": sid,
            "ledger_id": row["id"],
            "bucket": BUCKET_BY_ID.get(row["id"]),
            "stub": is_stub(row.get("notes") or ""),
        }
    cfg_json = json.dumps(cfg, ensure_ascii=False)
    if existing:
        conn.execute(
            """
            UPDATE strategy_defs SET
              display_name=?, markets_json=?, config_json=?, config_fingerprint=?,
              status='shadow', is_default=0, notes=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (display, '["ah"]', cfg_json, fp, notes, sid),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO strategy_defs (
              strategy_key, version, display_name, markets_json, config_json,
              config_fingerprint, status, is_default, notes
            ) VALUES (?,?,?,?,?,?, 'shadow', 0, ?)
            """,
            (key, VERSION, display, '["ah"]', cfg_json, fp, notes),
        )
        sid = int(cur.lastrowid)
    return {
        "action": action,
        "strategy_key": key,
        "id": sid,
        "ledger_id": row["id"],
        "bucket": BUCKET_BY_ID.get(row["id"]),
        "stub": is_stub(row.get("notes") or ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    ap.add_argument("--csv", type=Path, default=CSV_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pending, skipped = load_pending_rows(args.csv)
    conn = connect(args.db)
    try:
        results, guarded = [], []
        for r in pending:
            why = guard_reason(conn, r)
            if why:
                guarded.append({"id": r["id"], "strategy_key": strategy_key_for(r["id"]), "reason": why})
                continue
            results.append(upsert(conn, r, dry_run=args.dry_run))
        if not args.dry_run:
            conn.commit()
        # 断言未碰 V3 / MIRROR 预测数
        v3 = conn.execute(
            "SELECT COUNT(*) AS c FROM predictions WHERE strategy='CFFXDJ_5_V3'"
        ).fetchone()["c"]
        mir = conn.execute(
            "SELECT COUNT(*) AS c FROM predictions WHERE strategy='TEST_V3_MIRROR'"
        ).fetchone()["c"]
        shadow_preds = conn.execute(
            """
            SELECT COUNT(*) AS c FROM predictions
            WHERE strategy LIKE 'SHADOW_%'
            """
        ).fetchone()["c"]
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "version": VERSION,
                    "seeded": results,
                    "skipped_guard": guarded,
                    "skipped_csv": [
                        {"id": r["id"], "status": r["status"], "reason": "not pending_shadow"}
                        for r in skipped
                    ],
                    "predictions_cffxdj5v3": v3,
                    "predictions_test_v3_mirror": mir,
                    "predictions_shadow_keys": shadow_preds,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
