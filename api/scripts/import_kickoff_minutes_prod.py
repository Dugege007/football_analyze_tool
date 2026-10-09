#!/usr/bin/env python3
"""把 v2d3 的 kickoff_at + kickoff_minute_known(+kickoff_hour) 一次性导入现网。

拍板（足球分析师 2026-10-07）：
- 仅写 matches 开赛字段；不写 odds_asian / odds_snapshot / 澳门水位 / 1X2
- DUAL_WRITE_ODDS_ASIAN 保持关（本脚本永不设置该开关）
- V3 哈希须保持 <指纹已移除>；predictions 不动
- 先 bak app.db；支持 --rollback

用法：
  .venv/bin/python scripts/import_kickoff_minutes_prod.py dry-run
  .venv/bin/python scripts/import_kickoff_minutes_prod.py apply
  .venv/bin/python scripts/import_kickoff_minutes_prod.py rollback --bak backups/kickoff-prod-import-YYYYMMDDTHHMMSS+0800
  .venv/bin/python scripts/import_kickoff_minutes_prod.py verify
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
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD_DB = ROOT / "data" / "app.db"
DEFAULT_SRC = ROOT / "data" / "v2d3" / "app.db"
BACKUP_ROOT = ROOT / "backups"
OUT_DIR = Path(str(_ODDS_DATA_DIR / "backfill/kickoff-minute-fill-2026-10-07"))
REPORT_DIR = OUT_DIR / "reports"
NOTE_PATH = Path(str(_REPO_ROOT / "docs/schema/v2_0-kickoff-minute-fill.md"))
TZ_CN = timezone(timedelta(hours=8))
# 公开仓：真实库的 V3 预测哈希不公开；作者本机通过环境变量 EXPECTED_V3_HASH 提供，未设置则跳过该项核对
EXPECTED_V3 = os.environ.get("EXPECTED_V3_HASH", "").strip() or None
EXPECTED_N = 177
SOURCE = "v2d3_kickoff_import"
sys.path.insert(0, str(ROOT / "scripts"))
from validate_kickoff_placeholder import plan_check  # noqa: E402  (0.3.17 导入前占位符校验)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _v3_hash(db: Path) -> str:
    c = sqlite3.connect(db)
    try:
        a = list(c.execute("SELECT * FROM predictions WHERE strategy='CFFXDJ_5_V3' ORDER BY id"))
        d = list(c.execute("SELECT * FROM strategy_defs WHERE id=1"))
        return hashlib.sha256(repr((a, d)).encode()).hexdigest()[:16]
    finally:
        c.close()


def _oa_fp(db: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    from scripts.sync_odds_asian_from_snapshot import oa_fingerprint

    c = sqlite3.connect(db)
    try:
        return oa_fingerprint(c, exclude_probe=True)
    finally:
        c.close()


def _dual_write_env() -> str:
    return os.environ.get("DUAL_WRITE_ODDS_ASIAN", "0")


def _known_stats(db: Path) -> dict:
    c = sqlite3.connect(db)
    try:
        rows = dict(
            c.execute(
                "SELECT kickoff_minute_known, COUNT(*) FROM matches "
                "WHERE match_uid NOT LIKE 'probe:%' GROUP BY 1"
            )
        )
        n = c.execute(
            "SELECT COUNT(*) FROM matches WHERE match_uid NOT LIKE 'probe:%'"
        ).fetchone()[0]
        return {
            "base_n": n,
            "known_1": int(rows.get(1, 0)),
            "known_0": int(rows.get(0, 0)),
            "by_known": {str(k): v for k, v in rows.items()},
        }
    finally:
        c.close()


def _pred_count(db: Path) -> dict:
    c = sqlite3.connect(db)
    try:
        return {
            "v3": c.execute(
                "SELECT COUNT(*) FROM predictions WHERE strategy='CFFXDJ_5_V3'"
            ).fetchone()[0],
            "all": c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
            "odds_asian": c.execute("SELECT COUNT(*) FROM odds_asian").fetchone()[0],
        }
    finally:
        c.close()


def _build_plan(src: Path, dst: Path) -> list[dict]:
    sc = sqlite3.connect(src)
    dc = sqlite3.connect(dst)
    sc.row_factory = sqlite3.Row
    dc.row_factory = sqlite3.Row
    try:
        src_rows = {
            r["id"]: r
            for r in sc.execute(
                "SELECT id, match_uid, jingcai_date, kickoff_at, kickoff_minute_known, kickoff_hour "
                "FROM matches WHERE match_uid NOT LIKE 'probe:%'"
            )
        }
        dst_rows = {
            r["id"]: r
            for r in dc.execute(
                "SELECT id, match_uid, jingcai_date, kickoff_at, kickoff_minute_known, kickoff_hour "
                "FROM matches WHERE match_uid NOT LIKE 'probe:%'"
            )
        }
        plans: list[dict] = []
        for mid, d in sorted(dst_rows.items()):
            s = src_rows.get(mid)
            if s is None:
                plans.append(
                    {
                        "match_id": mid,
                        "match_uid": d["match_uid"],
                        "action": "skip_missing_src",
                        "reason": "not in v2d3 base",
                    }
                )
                continue
            if s["match_uid"] != d["match_uid"]:
                plans.append(
                    {
                        "match_id": mid,
                        "match_uid": d["match_uid"],
                        "src_uid": s["match_uid"],
                        "action": "skip_uid_mismatch",
                        "reason": "match_uid differs",
                    }
                )
                continue
            if int(s["kickoff_minute_known"] or 0) != 1 or not s["kickoff_at"]:
                plans.append(
                    {
                        "match_id": mid,
                        "match_uid": d["match_uid"],
                        "action": "skip_src_unknown",
                        "reason": "v2d3 known!=1 or empty kickoff",
                    }
                )
                continue
            # 0.3.17 导入前校验：5DF 12:00 占位符（对现网竞彩整点）
            ko_chk = plan_check(s["kickoff_at"], d["jingcai_date"], d["kickoff_hour"])
            if ko_chk["hold"]:
                plans.append({"match_id": mid, "match_uid": d["match_uid"],
                              "old_kickoff_at": d["kickoff_at"], "new_kickoff_at": s["kickoff_at"],
                              "action": "hold_kickoff_placeholder", "kickoff_check": ko_chk,
                              "source": SOURCE})
                continue
            if s["kickoff_hour"] is not None:
                new_hour = int(s["kickoff_hour"])
            else:
                new_hour = int(s["kickoff_at"][11:13])
            old_hour = (
                int(d["kickoff_hour"]) if d["kickoff_hour"] is not None else None
            )
            already = (
                int(d["kickoff_minute_known"] or 0) == 1
                and d["kickoff_at"] == s["kickoff_at"]
                and old_hour == new_hour
            )
            plans.append(
                {
                    "match_id": mid,
                    "match_uid": d["match_uid"],
                    "old_kickoff_at": d["kickoff_at"],
                    "new_kickoff_at": s["kickoff_at"],
                    "old_minute_known": int(d["kickoff_minute_known"] or 0),
                    "new_minute_known": 1,
                    "old_kickoff_hour": d["kickoff_hour"],
                    "new_kickoff_hour": new_hour,
                    "hour_changed": old_hour != new_hour,
                    "action": "noop" if already else "update",
                    "source": SOURCE,
                }
            )
        # src-only base rows (should be 0 for 177)
        for mid, s in src_rows.items():
            if mid not in dst_rows:
                plans.append(
                    {
                        "match_id": mid,
                        "match_uid": s["match_uid"],
                        "action": "skip_src_only",
                        "reason": "in v2d3 but not prod (not imported)",
                    }
                )
        return plans
    finally:
        sc.close()
        dc.close()


def _stamp() -> str:
    return datetime.now(TZ_CN).strftime("%Y%m%dT%H%M%S%z")


def _backup_prod(stamp: str) -> Path:
    bak_dir = BACKUP_ROOT / f"kickoff-prod-import-{stamp}"
    bak_dir.mkdir(parents=True, exist_ok=False)
    dest = bak_dir / "app.db"
    # online-safe snapshot via sqlite backup API
    src = sqlite3.connect(PROD_DB)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    meta = {
        "created_at": datetime.now(TZ_CN).isoformat(),
        "source": str(PROD_DB),
        "sha256": _sha256(dest),
        "v3_hash": _v3_hash(dest),
        "oa": _oa_fp(dest),
        "known": _known_stats(dest),
        "counts": _pred_count(dest),
        "dual_write_env": _dual_write_env(),
        "note": "full app.db snapshot before kickoff prod import; rollback can restore whole file or kickoff fields only",
    }
    (bak_dir / "META.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return bak_dir


def _save_kickoff_snapshot(db: Path, path: Path) -> int:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT id AS match_id, match_uid, kickoff_at, kickoff_minute_known, kickoff_hour "
                "FROM matches ORDER BY id"
            )
        ]
    finally:
        c.close()
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def _apply_updates(db: Path, plans: list[dict]) -> int:
    updates = [p for p in plans if p["action"] == "update"]
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(db)
    try:
        cur = con.cursor()
        for p in updates:
            cur.execute(
                """
                UPDATE matches
                   SET kickoff_at = ?,
                       kickoff_minute_known = 1,
                       kickoff_hour = ?,
                       updated_at = ?
                 WHERE id = ? AND match_uid = ?
                """,
                (
                    p["new_kickoff_at"],
                    p["new_kickoff_hour"],
                    now,
                    p["match_id"],
                    p["match_uid"],
                ),
            )
            if cur.rowcount != 1:
                con.rollback()
                raise SystemExit(
                    f"UPDATE rowcount!=1 for match_id={p['match_id']} uid={p['match_uid']}"
                )
        con.commit()
    finally:
        con.close()
    return len(updates)


def _write_acceptance(payload: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "ACCEPTANCE_prod_kickoff_import.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _append_note(section: str) -> None:
    if not NOTE_PATH.exists():
        NOTE_PATH.write_text("# 开赛分钟回填\n\n", encoding="utf-8")
    text = NOTE_PATH.read_text(encoding="utf-8")
    marker = "## 现网导入（kickoff only · 2026-10-07）"
    if marker in text:
        # replace from marker to EOF or next ## at start after marker
        idx = text.index(marker)
        text = text[:idx] + section.rstrip() + "\n"
    else:
        text = text.rstrip() + "\n\n" + section.rstrip() + "\n"
    NOTE_PATH.write_text(text, encoding="utf-8")


def cmd_dry_run(args: argparse.Namespace) -> int:
    plans = _build_plan(Path(args.src), PROD_DB)
    updates = [p for p in plans if p["action"] == "update"]
    hour_ch = [p for p in updates if p.get("hour_changed")]
    summary = {
        "mode": "dry-run",
        "src": str(args.src),
        "dst": str(PROD_DB),
        "planned_updates": len(updates),
        "hour_changed": len(hour_ch),
        "hour_changed_ids": [p["match_id"] for p in hour_ch],
        "skips": sum(1 for p in plans if str(p["action"]).startswith("skip")),
        "noop": sum(1 for p in plans if p["action"] == "noop"),
        "before": {
            "known": _known_stats(PROD_DB),
            "v3_hash": _v3_hash(PROD_DB),
            "oa": _oa_fp(PROD_DB),
            "counts": _pred_count(PROD_DB),
            "dual_write_env": _dual_write_env(),
            "sha256": _sha256(PROD_DB),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if _dual_write_env().strip().lower() in ("1", "true", "yes", "on"):
        print(
            "REFUSE: DUAL_WRITE_ODDS_ASIAN is on; this pack requires it off",
            file=sys.stderr,
        )
        return 2

    src = Path(args.src)
    if not src.exists() or not PROD_DB.exists():
        raise SystemExit("src or prod db missing")

    before = {
        "known": _known_stats(PROD_DB),
        "v3_hash": _v3_hash(PROD_DB),
        "oa": _oa_fp(PROD_DB),
        "counts": _pred_count(PROD_DB),
        "dual_write_env": _dual_write_env(),
        "sha256": _sha256(PROD_DB),
    }
    if EXPECTED_V3 and before["v3_hash"] != EXPECTED_V3:
        raise SystemExit(
            f"precheck V3 hash {before['v3_hash']} != expected {EXPECTED_V3}"
        )
    if before["known"]["base_n"] != EXPECTED_N:
        raise SystemExit(f"precheck base_n={before['known']['base_n']} != {EXPECTED_N}")

    plans = _build_plan(src, PROD_DB)
    updates = [p for p in plans if p["action"] == "update"]
    skips = [p for p in plans if str(p["action"]).startswith("skip")]
    if skips:
        raise SystemExit(f"refuse: unexpected skips={len(skips)} sample={skips[:3]}")
    if len(updates) + sum(1 for p in plans if p["action"] == "noop") != EXPECTED_N:
        raise SystemExit(
            f"refuse: plan cover {len(updates)}+noop != {EXPECTED_N}"
        )

    stamp = _stamp()
    bak_dir = _backup_prod(stamp)
    # field-level snapshot for precise rollback without full file replace
    kickoff_before = bak_dir / "kickoff_fields_before.json"
    _save_kickoff_snapshot(PROD_DB, kickoff_before)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = OUT_DIR / "logs" / f"kickoff_prod_import_plan_{stamp}.jsonl"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("w", encoding="utf-8") as f:
        for p in plans:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    n_upd = _apply_updates(PROD_DB, plans)

    after = {
        "known": _known_stats(PROD_DB),
        "v3_hash": _v3_hash(PROD_DB),
        "oa": _oa_fp(PROD_DB),
        "counts": _pred_count(PROD_DB),
        "dual_write_env": _dual_write_env(),
        "sha256": _sha256(PROD_DB),
    }

    ok = (
        after["known"]["known_1"] == EXPECTED_N
        and after["known"]["known_0"] == 0
        and after["known"]["base_n"] == EXPECTED_N
        and (not EXPECTED_V3 or after["v3_hash"] == EXPECTED_V3)
        and after["oa"] == before["oa"]
        and after["counts"] == before["counts"]
        and after["dual_write_env"] == before["dual_write_env"]
    )

    hour_ch = [p for p in updates if p.get("hour_changed")]
    acceptance = {
        "task": "kickoff-minute-prod-import",
        "finished_at": datetime.now(TZ_CN).isoformat(),
        "ok": ok,
        "src": str(src),
        "dst": str(PROD_DB),
        "bak_dir": str(bak_dir),
        "bak_app_db": str(bak_dir / "app.db"),
        "kickoff_fields_before": str(kickoff_before),
        "plan_log": str(plan_path),
        "updated": n_upd,
        "noop": sum(1 for p in plans if p["action"] == "noop"),
        "hour_changed": len(hour_ch),
        "hour_changed_match_ids": [p["match_id"] for p in hour_ch],
        "hour_policy": (
            "kickoff_hour synced to new kickoff_at Beijing hour for sort compatibility; "
            "3 rows change hour; V3 hash uses predictions+strategy_defs#1 only — unaffected"
        ),
        "not_imported": [
            "odds_asian",
            "odds_snapshot",
            "macau_water",
            "euro_1x2",
            "predictions",
        ],
        "dual_write_odds_asian": after["dual_write_env"],
        "before": before,
        "after": after,
        "expected_v3": EXPECTED_V3,
        "rollback_kickoff_fields": (
            f".venv/bin/python scripts/import_kickoff_minutes_prod.py rollback "
            f"--bak {bak_dir} --mode fields"
        ),
        "rollback_full_db": (
            f".venv/bin/python scripts/import_kickoff_minutes_prod.py rollback "
            f"--bak {bak_dir} --mode full"
        ),
        "script": str(Path(__file__).resolve()),
        "note_path": str(NOTE_PATH),
    }
    acc_path = _write_acceptance(acceptance)
    acceptance["acceptance_path"] = str(acc_path)
    acc_path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")

    section = f"""## 现网导入（kickoff only · 2026-10-07）

**拍板**：一次性导入 v2d3 → 现网 `kickoff_at` + `kickoff_minute_known=1`（并同步 `kickoff_hour`）；**不开** DUAL_WRITE；**不同步**澳门水位 / mid / 1X2。

| 项 | 值 |
|---|---|
| 更新行 | **{n_upd}** / {EXPECTED_N} |
| 现网 known=1 | **{after['known']['known_1']}** / {EXPECTED_N} |
| V3 哈希 | `{after['v3_hash']}`（期望 `{EXPECTED_V3}`，{'OK' if after['v3_hash']==EXPECTED_V3 else 'FAIL'}） |
| OA 指纹 | `{after['oa']['fp']}` count={after['oa']['count']}（与导入前一致={'OK' if after['oa']==before['oa'] else 'FAIL'}） |
| predictions | V3={after['counts']['v3']} / all={after['counts']['all']}（不变） |
| odds_asian | {after['counts']['odds_asian']}（不变） |
| DUAL_WRITE_ODDS_ASIAN | `{after['dual_write_env']}` |
| kickoff_hour 变更 | **{len(hour_ch)}** 场（ids={ [p['match_id'] for p in hour_ch] }）；仅为与新 `kickoff_at` 北京小时一致，不影响 V3 |
| bak | `{bak_dir}` |
| ACCEPTANCE | `{acc_path}` |

### 回滚

```bash
cd api
# 仅恢复 kickoff 三字段（推荐）
.venv/bin/python scripts/import_kickoff_minutes_prod.py rollback --bak {bak_dir} --mode fields
# 或整库替换
.venv/bin/python scripts/import_kickoff_minutes_prod.py rollback --bak {bak_dir} --mode full
```

### 验收

```bash
cd api
.venv/bin/python scripts/import_kickoff_minutes_prod.py verify
.venv/bin/python scripts/v3_hash.py data/app.db   # expect {EXPECTED_V3}
```
"""
    _append_note(section)
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    bak = Path(args.bak)
    if not bak.is_dir():
        raise SystemExit(f"bak dir not found: {bak}")
    mode = args.mode
    stamp = _stamp()
    # safety bak of current prod before rollback
    safety = BACKUP_ROOT / f"kickoff-prod-pre-rollback-{stamp}"
    safety.mkdir(parents=True, exist_ok=False)
    src = sqlite3.connect(PROD_DB)
    dst = sqlite3.connect(safety / "app.db")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    (safety / "META.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(TZ_CN).isoformat(),
                "reason": "pre-rollback safety",
                "rolling_back_from": str(bak),
                "mode": mode,
                "sha256": _sha256(safety / "app.db"),
                "v3_hash": _v3_hash(safety / "app.db"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if mode == "full":
        bak_db = bak / "app.db"
        if not bak_db.exists():
            raise SystemExit(f"missing {bak_db}")
        shutil.copy2(bak_db, PROD_DB)
        result = {"mode": "full", "restored_from": str(bak_db), "safety": str(safety)}
    else:
        fields_path = bak / "kickoff_fields_before.json"
        if not fields_path.exists():
            raise SystemExit(f"missing {fields_path}")
        rows = json.loads(fields_path.read_text(encoding="utf-8"))
        now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        con = sqlite3.connect(PROD_DB)
        try:
            cur = con.cursor()
            n = 0
            for r in rows:
                cur.execute(
                    """
                    UPDATE matches
                       SET kickoff_at = ?,
                           kickoff_minute_known = ?,
                           kickoff_hour = ?,
                           updated_at = ?
                     WHERE id = ? AND match_uid = ?
                    """,
                    (
                        r["kickoff_at"],
                        r["kickoff_minute_known"],
                        r["kickoff_hour"],
                        now,
                        r["match_id"],
                        r["match_uid"],
                    ),
                )
                n += cur.rowcount
            con.commit()
        finally:
            con.close()
        result = {
            "mode": "fields",
            "restored_rows_touched": n,
            "from": str(fields_path),
            "safety": str(safety),
        }

    result.update(
        {
            "after_known": _known_stats(PROD_DB),
            "after_v3": _v3_hash(PROD_DB),
            "after_oa": _oa_fp(PROD_DB),
            "dual_write_env": _dual_write_env(),
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    known = _known_stats(PROD_DB)
    v3 = _v3_hash(PROD_DB)
    oa = _oa_fp(PROD_DB)
    dual = _dual_write_env()
    counts = _pred_count(PROD_DB)
    out = {
        "known": known,
        "v3_hash": v3,
        "v3_ok": (v3 == EXPECTED_V3) if EXPECTED_V3 else None,
        "oa": oa,
        "dual_write_env": dual,
        "counts": counts,
        "known_ok": known["known_1"] == EXPECTED_N and known["base_n"] == EXPECTED_N,
        "dual_ok": dual.strip().lower() not in ("1", "true", "yes", "on"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["v3_ok"] and out["known_ok"] and out["dual_ok"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--src",
        default=str(DEFAULT_SRC),
        help="source replica db (default v2d3)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dry-run")
    p_dry.set_defaults(func=cmd_dry_run)

    p_apply = sub.add_parser("apply")
    p_apply.set_defaults(func=cmd_apply)

    p_rb = sub.add_parser("rollback")
    p_rb.add_argument("--bak", required=True, help="backup dir from apply")
    p_rb.add_argument(
        "--mode",
        choices=("fields", "full"),
        default="fields",
        help="fields=restore kickoff columns only; full=replace app.db",
    )
    p_rb.set_defaults(func=cmd_rollback)

    p_v = sub.add_parser("verify")
    p_v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    # propagate src onto namespace for subcommands that need it
    if not hasattr(args, "src"):
        args.src = str(DEFAULT_SRC)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
