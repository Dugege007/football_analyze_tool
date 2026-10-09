#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dc_train 队名别名导入（草案，2026-10-08）。默认 dry-run，只读打开数据库。

用法
  # 1) 预演（默认；mode=ro 打开，不写库）
  python3 import_dc_aliases.py --db /path/to/app.db
  # 2) 正式写入（需显式 --apply；先自动备份，再单事务写入，写完出 manifest 供回滚）
  python3 import_dc_aliases.py --db /path/to/app.db --apply
  # 3) KuPS 合并（单独开关，默认不做；--apply 才写）
  python3 import_dc_aliases.py --db ... --merge-kups [--apply]
  # 4) 回滚：按 manifest 删除本批新增（只删本批插入且未被 matches 引用的行），或整库还原备份
  python3 import_dc_aliases.py --db ... --rollback manifest-XXXX.json [--apply]
  cp app.db.bak-pre-dc-aliases-XXXX app.db       # 整库还原（停 API 后）

规则
- 只做 INSERT OR IGNORE / skip-existing：已有 alias 指向同一队 → 跳过；指向别的队 → 记冲突、不写。
- 新队：teams 插入规范名（note 记 league / 来源 / 批次），并按现网惯例给规范名自身建 alias。
- 默认跳过 conflicts.csv 里 severity=blocker 的行，再套 corrections.csv 里 apply=yes 的修正。
- 默认只收 high+med；--min-confidence high 可只导 high。
- 不改 matches / predictions；KuPS 合并只改 matches 的 *_team_id 与 alias 指向，冻结预测行不动。
- DUAL_WRITE 与本脚本无关；本脚本不碰 v2d3 以外的任何路径，除非 --db 指定。
"""
# --- public repo: paths are env-overridable (see config.example.env) ---
import os as _rp_os
from pathlib import Path as _RpPath
_REPO_ROOT = _RpPath(__file__).resolve().parents[3]
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

import argparse, csv, json, shutil, sqlite3, sys, unicodedata, re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROP = Path(str(_ODDS_DATA_DIR / "research/dc_train/team_aliases_proposed.csv"))
BATCH = "dc_train_team_map_20261008"
KUPS_KEEP, KUPS_DROP = 34, 2


def norm(name):  # 与 match-analysis-api/scripts/import_lib.normalize_name 一致
    n = unicodedata.normalize("NFKC", str(name)).strip()
    return re.sub(r"\s+", " ", n) or None


MANUAL_JSON = sorted(Path(str(_ODDS_DATA_DIR / "imports/2026")).glob("*.json"))


def user_main_names(db):
    """用户旧手工数据里的主名：现网 teams.name_zh_canonical + 旧手工月度 JSON 的 match.teams.home/away。"""
    names = {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for (n,) in c.execute("SELECT name_zh_canonical FROM teams"):
        names[n] = "prod_teams"
    c.close()
    for f in MANUAL_JSON:
        try:
            for it in json.load(open(f, encoding="utf-8")):
                for s in ("home", "away"):
                    n = it["match"]["teams"][s]
                    names.setdefault(n, f"manual_json:{f.name}")
        except Exception:
            pass
    return names


def load_plan(min_conf, conflicts_path, corrections_path, db=None):
    rows = list(csv.DictReader(open(PROP, encoding="utf-8")))
    umain = user_main_names(db) if db else {}
    guard_log = []
    block = set()
    if conflicts_path and Path(conflicts_path).exists():
        for r in csv.DictReader(open(conflicts_path, encoding="utf-8-sig")):
            if r["severity"] == "blocker":
                block.add((r["league_name_zh"], r["alias"]))
    corr, renames, drops = {}, {}, {}
    if corrections_path and Path(corrections_path).exists():
        for r in csv.DictReader(open(corrections_path, encoding="utf-8")):
            if r["apply"].strip().lower() != "yes":
                continue
            if r["new_action"] == "rename_canonical":
                renames[r["alias"]] = r      # key = 旧规范名
            elif r["new_action"] == "drop_alias":
                drops[(r["league_name_zh"], r["alias"])] = r   # 这个写法不进 team_aliases（查找是全局的，无法按联赛限定）
            else:
                corr[(r["league_name_zh"], r["alias"])] = r
    keep = {"high"} if min_conf == "high" else {"high", "med"}
    plan, skipped = [], []
    for r in rows:
        k = (r["league_name_zh"], r["alias"])
        if r["action"] == "merge_teams":
            skipped.append((r, "merge_teams 走 --merge-kups")); continue
        if r["confidence"] not in keep:
            skipped.append((r, f"confidence={r['confidence']}")); continue
        if k in corr:
            c = corr.pop(k)
            r = dict(r, action=c["new_action"], team_canonical=c["new_team_canonical"],
                     team_id=c["new_team_id"], evidence=r["evidence"] + f" | corrected: {c['reason']}")
        elif k in block:
            skipped.append((r, "conflicts.csv blocker 且无修正")); continue
        plan.append(r)
    # 规范名改名（分析师定稿）：前提——旧名若已是用户主名，则不改主名，只把新名作 alias（记联赛）
    extra = []
    for old, c in renames.items():
        new, lg = c["new_team_canonical"], c["league_name_zh"]
        hit_old, hit_new = umain.get(old), umain.get(new)
        if hit_old:
            guard_log.append(dict(old=old, new=new, league=lg, kept_user_main=old, why=hit_old))
            extra.append(dict(action="new_team", alias=new, team_canonical=old, team_id="", league_name_zh=lg,
                              confidence="manual", evidence=f"disambig alias; user main name kept ({hit_old})",
                              model_source_name="", source=BATCH))
            continue
        guard_log.append(dict(old=old, new=new, league=lg, kept_user_main=None,
                              why=f"旧名不在用户主名里{'；新名已是用户主名 ' + hit_new if hit_new else ''}"))
        n = 0
        for r in plan:
            if r["team_canonical"] == old:
                r["team_canonical"] = new
                r["evidence"] += f" | renamed {old}→{new}"
                n += 1
        if n == 0:
            guard_log[-1]["why"] += "；提议里没有这个规范名"
    plan.extend(extra)
    for (lg, al), c in drops.items():
        n = 0
        for r in plan:
            if r["league_name_zh"] == lg and r["alias"] == al:
                r["no_alias"] = "1"; n += 1
        guard_log.append(dict(drop_alias=al, league=lg, rows_marked=n,
                              prod_has_it=umain.get(al), note="不写入 team_aliases；若现网已有此名则不动"))
    for k, c in corr.items():  # corrections 里的新增行（原提议没有）
        if c["new_action"] in ("add_alias", "new_team"):
            plan.append(dict(action=c["new_action"], alias=c["alias"], team_canonical=c["new_team_canonical"],
                             team_id=c["new_team_id"], league_name_zh=c["league_name_zh"], confidence="manual",
                             evidence=f"manual: {c['reason']}", model_source_name="", source=BATCH))
    return plan, skipped, guard_log


def lookup(conn, name):
    r = conn.execute("SELECT team_id FROM team_aliases WHERE alias=?", (name,)).fetchone()
    if r: return r[0]
    r = conn.execute("SELECT id FROM teams WHERE name_zh_canonical=?", (name,)).fetchone()
    return r[0] if r else None


def backup(db):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    dst = Path(db).with_name(Path(db).name + f".bak-pre-dc-aliases-{ts}")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out); out.close(); src.close()
    ok = sqlite3.connect(f"file:{dst}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        sys.exit(f"备份完整性检查失败：{dst} -> {ok}")
    return dst, ts


def run_import(conn, plan, apply):
    """返回 (ops, conflicts, inserted)。apply=False 时 conn 为只读，只模拟。"""
    ops, conflicts = [], []
    inserted = {"teams": [], "team_aliases": []}
    sim_alias, sim_team, next_id = {}, {}, -1   # dry-run 里模拟新建
    def find(name):
        return sim_alias.get(name) or sim_team.get(name) or lookup(conn, name)
    def find_alias(name):  # 只查 alias 表（规范名自身 alias 用）
        if name in sim_alias: return sim_alias[name]
        r = conn.execute("SELECT team_id FROM team_aliases WHERE alias=?", (name,)).fetchone()
        return r[0] if r else None
    def put_alias(alias, tid, why, league=""):
        alias = norm(alias)
        cur = find_alias(alias) if why == "canonical_self" else (find_alias(alias) or find(alias))
        if cur == tid:
            ops.append(("skip_existing_alias", alias, tid, why)); return
        if cur is not None:
            conflicts.append(("alias_points_elsewhere", alias, tid, cur, why)); return
        if apply:
            c = conn.execute("INSERT OR IGNORE INTO team_aliases(alias, team_id, source) VALUES (?,?,?)",
                             (alias, tid, f"{BATCH}|league={league}" if league else BATCH))
            if c.rowcount: inserted["team_aliases"].append(c.lastrowid)
        else:
            sim_alias[alias] = tid
        ops.append(("insert_alias", alias, tid, why))
    for r in plan:
        canon = norm(r["team_canonical"])
        if r["action"] == "add_alias":
            tid = int(r["team_id"]) if r["team_id"] else find(canon)
            real = conn.execute("SELECT name_zh_canonical FROM teams WHERE id=?", (tid,)).fetchone() if tid and tid > 0 else None
            if not real:
                conflicts.append(("add_alias_target_missing", r["alias"], r["team_id"], None, r["league_name_zh"])); continue
            put_alias(r["alias"], tid, r["league_name_zh"], r["league_name_zh"])
        elif r["action"] == "new_team":
            tid = find(canon)
            if tid is None:   # skip-existing：规范名已存在就复用
                note = f"{BATCH}|league={r['league_name_zh']}|src={r.get('model_source_name','')}|conf={r['confidence']}"
                if apply:
                    c = conn.execute("INSERT OR IGNORE INTO teams(name_zh_canonical, note) VALUES (?,?)", (canon, note))
                    tid = c.lastrowid if c.rowcount else lookup(conn, canon)
                    if c.rowcount: inserted["teams"].append(tid)
                else:
                    tid = next_id; next_id -= 1; sim_team[canon] = tid
                ops.append(("insert_team", canon, tid, r["league_name_zh"]))
                put_alias(canon, tid, "canonical_self", r["league_name_zh"])
            else:
                ops.append(("reuse_team", canon, tid, r["league_name_zh"]))
            if r.get("no_alias"):
                ops.append(("drop_alias_not_written", norm(r["alias"]), tid, r["league_name_zh"]))
            elif norm(r["alias"]) != canon:
                put_alias(r["alias"], tid, r["league_name_zh"], r["league_name_zh"])
        else:
            conflicts.append(("unknown_action", r["alias"], r["action"], None, ""))
    return ops, conflicts, inserted


def merge_kups(conn, apply):
    q = lambda s, *a: conn.execute(s, a).fetchall()
    keep = q("SELECT name_zh_canonical FROM teams WHERE id=?", KUPS_KEEP)
    drop = q("SELECT name_zh_canonical FROM teams WHERE id=?", KUPS_DROP)
    drop_note = q("SELECT note FROM teams WHERE id=?", KUPS_DROP)
    if not keep or not drop or (drop_note and (drop_note[0][0] or "").startswith("merged_into:")):
        return {"status": "already_merged_or_missing", "keep": keep, "drop": drop}
    plan = {
        "keep": (KUPS_KEEP, keep[0][0]), "drop": (KUPS_DROP, drop[0][0]),
        "matches_home": q("SELECT id, match_uid FROM matches WHERE home_team_id=?", KUPS_DROP),
        "matches_away": q("SELECT id, match_uid FROM matches WHERE away_team_id=?", KUPS_DROP),
        "aliases": q("SELECT id, alias FROM team_aliases WHERE team_id=?", KUPS_DROP),
        "predictions_untouched": q("SELECT p.match_id, p.strategy FROM predictions p JOIN matches m ON m.id=p.match_id "
                                   "WHERE m.home_team_id=? OR m.away_team_id=?", KUPS_DROP, KUPS_DROP),
    }
    if apply:
        conn.execute("UPDATE matches SET home_team_id=? WHERE home_team_id=?", (KUPS_KEEP, KUPS_DROP))
        conn.execute("UPDATE matches SET away_team_id=? WHERE away_team_id=?", (KUPS_KEEP, KUPS_DROP))
        conn.execute("UPDATE team_aliases SET team_id=? WHERE team_id=?", (KUPS_KEEP, KUPS_DROP))
        # 软合并：不 DELETE teams 行（避免 ON DELETE CASCADE 风险）；改名腾出 UNIQUE，并在 note 记去向
        conn.execute("UPDATE teams SET name_zh_canonical=name_zh_canonical||'#merged→34', note=? WHERE id=?",
                     (f"merged_into:{KUPS_KEEP}|{BATCH}", KUPS_DROP))
    return plan


def rollback(conn, manifest, apply):
    m = json.load(open(manifest, encoding="utf-8"))
    out = {"aliases": 0, "teams": 0, "teams_kept_referenced": []}
    ins = m.get("inserted") or {"team_aliases": [], "teams": []}
    ids_a, ids_t = ins["team_aliases"], ins["teams"]
    for tid in ids_t:
        n = conn.execute("SELECT COUNT(*) FROM matches WHERE home_team_id=? OR away_team_id=?", (tid, tid)).fetchone()[0]
        if n: out["teams_kept_referenced"].append(tid)
    if apply:
        conn.executemany("DELETE FROM team_aliases WHERE id=? AND source LIKE ?", [(i, BATCH + "%") for i in ids_a])
        for tid in ids_t:
            if tid in out["teams_kept_referenced"]: continue
            conn.execute("DELETE FROM team_aliases WHERE team_id=? AND source LIKE ?", (tid, BATCH + "%"))
            conn.execute("DELETE FROM teams WHERE id=? AND note LIKE ?", (tid, f"{BATCH}%"))
        k = m.get("kups")
        if k and k.get("applied"):
            conn.execute("UPDATE teams SET name_zh_canonical=?, note=NULL WHERE id=?", (k["drop"][1], KUPS_DROP))
            for mid, _ in k["matches_home"]: conn.execute("UPDATE matches SET home_team_id=? WHERE id=?", (KUPS_DROP, mid))
            for mid, _ in k["matches_away"]: conn.execute("UPDATE matches SET away_team_id=? WHERE id=?", (KUPS_DROP, mid))
            for aid, _ in k["aliases"]: conn.execute("UPDATE team_aliases SET team_id=? WHERE id=?", (KUPS_DROP, aid))
    out["aliases"], out["teams"] = len(ids_a), len(ids_t) - len(out["teams_kept_referenced"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true", help="真正写入；不加就是 dry-run")
    ap.add_argument("--min-confidence", choices=["high", "med"], default="med")
    ap.add_argument("--conflicts", default=str(HERE / "conflicts.csv"))
    ap.add_argument("--corrections", default=str(HERE / "corrections.csv"))
    ap.add_argument("--merge-kups", action="store_true")
    ap.add_argument("--skip-aliases", action="store_true", help="只做 --merge-kups")
    ap.add_argument("--rollback")
    ap.add_argument("--yes-i-have-approval", action="store_true", help="写现网 app.db 必须带上（用户批准后）")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if a.apply and db.name == "app.db" and db.parent.name == "data" and not a.yes_i_have_approval:
        sys.exit("拒绝：写现网 app.db 需要用户批准，并加 --yes-i-have-approval")
    report = {"db": str(db), "mode": "apply" if a.apply else "dry-run", "batch": BATCH, "at": datetime.now().isoformat()}
    if a.apply:
        bak, ts = backup(db); report["backup"] = str(bak)
        conn = sqlite3.connect(db); conn.execute("BEGIN IMMEDIATE")
    else:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("teams", "team_aliases")}
    try:
        if a.rollback:
            report["rollback"] = rollback(conn, a.rollback, a.apply)
        else:
            if not a.skip_aliases:
                plan, skipped, guard = load_plan(a.min_confidence, a.conflicts, a.corrections, db)
                report["user_main_name_guard"] = guard
                ops, conflicts, inserted = run_import(conn, plan, a.apply)
                report.update(plan_rows=len(plan), skipped=[(s[0]["league_name_zh"], s[0]["alias"], s[1]) for s in skipped],
                              op_counts={k: sum(1 for o in ops if o[0] == k) for k in {o[0] for o in ops}},
                              conflicts=conflicts, inserted=inserted, ops=ops)
            if a.merge_kups:
                k = merge_kups(conn, a.apply); k["applied"] = bool(a.apply) and "matches_away" in k
                report["kups"] = k
        if a.apply:
            # 写后校验：无孤儿 alias、每个新队有自身 alias、matches 无孤儿 team_id
            orphan = conn.execute("SELECT COUNT(*) FROM team_aliases a LEFT JOIN teams t ON t.id=a.team_id WHERE t.id IS NULL").fetchone()[0]
            m_orphan = conn.execute("SELECT COUNT(*) FROM matches m LEFT JOIN teams t ON t.id=m.home_team_id "
                                    "WHERE m.home_team_id IS NOT NULL AND t.id IS NULL").fetchone()[0] + \
                       conn.execute("SELECT COUNT(*) FROM matches m LEFT JOIN teams t ON t.id=m.away_team_id "
                                    "WHERE m.away_team_id IS NOT NULL AND t.id IS NULL").fetchone()[0]
            if orphan or m_orphan:
                raise RuntimeError(f"写后校验失败 orphan_alias={orphan} orphan_match_team={m_orphan}")
            conn.commit()
    except Exception:
        if a.apply: conn.rollback()
        raise
    report["before"] = before
    report["after"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("teams", "team_aliases")}
    conn.close()
    step = "rollback" if a.rollback else ("kups" if a.skip_aliases else ("aliases+kups" if a.merge_kups else "aliases"))
    out = HERE / f"{'manifest' if a.apply else 'dryrun'}-{step}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.json"
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    summary = {k: report.get(k) for k in ("mode", "backup", "plan_rows", "op_counts", "before", "after")}
    summary["n_conflicts"] = len(report.get("conflicts", []))
    summary["n_skipped"] = len(report.get("skipped", []))
    summary["user_main_name_guard"] = report.get("user_main_name_guard")
    if "kups" in report:
        k = report["kups"]
        summary["kups"] = {x: k.get(x) for x in ("keep", "drop", "matches_home", "matches_away", "aliases", "applied")}
        summary["kups"]["predictions_untouched"] = len(k.get("predictions_untouched", []))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print("report:", out)


if __name__ == "__main__":
    main()
