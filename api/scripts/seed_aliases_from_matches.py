#!/usr/bin/env python3
"""从 matches 文本列幂等播种 teams/leagues + aliases，并回填 *_id。

用法:
  python scripts/seed_aliases_from_matches.py [--dry-run] [--db PATH]

- 每个 DISTINCT 非空中文名 → 一条规范名 + 同名 alias（source=seed_from_matches）
- UNIQUE(alias) 冲突且指向不同实体 → 记入 needs_review，不强制合并
- 轻量 NFKC/trim；不擅自改译名
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
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import DB_PATH, connect  # noqa: E402
from import_lib import (  # noqa: E402
    normalize_name,
    resolve_or_create_league,
    resolve_or_create_team,
)

REVIEW_PATH = Path(str(_ODDS_DATA_DIR / "backfill/alias-seed-needs-review-20261006.md"))


def _distinct(conn, col: str) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT {col} AS n FROM matches
        WHERE {col} IS NOT NULL AND TRIM({col}) != ''
        ORDER BY n
        """
    ).fetchall()
    return [r["n"] for r in rows]


def _counts(conn) -> dict[str, int]:
    q = [
        ("matches", "SELECT COUNT(*) FROM matches"),
        ("teams", "SELECT COUNT(*) FROM teams"),
        ("team_aliases", "SELECT COUNT(*) FROM team_aliases"),
        ("leagues", "SELECT COUNT(*) FROM leagues"),
        ("league_aliases", "SELECT COUNT(*) FROM league_aliases"),
        (
            "matches_all_ids",
            """SELECT COUNT(*) FROM matches
               WHERE home_team_id IS NOT NULL
                 AND away_team_id IS NOT NULL
                 AND league_id IS NOT NULL""",
        ),
        (
            "matches_home_id",
            "SELECT COUNT(*) FROM matches WHERE home_team_id IS NOT NULL",
        ),
        (
            "matches_away_id",
            "SELECT COUNT(*) FROM matches WHERE away_team_id IS NOT NULL",
        ),
        (
            "matches_league_id",
            "SELECT COUNT(*) FROM matches WHERE league_id IS NOT NULL",
        ),
    ]
    out: dict[str, int] = {}
    for k, sql in q:
        out[k] = int(conn.execute(sql).fetchone()[0])
    return out


def _near_dup_notes(names: list[str]) -> list[str]:
    """启发式列出可能需人工合并的近重复（繁简/错字）。"""
    notes: list[str] = []
    # 已知数据集内近重复（不自动合并）
    known_groups = [
        ["乌兹别克斯坦", "乌茲別克斯坦", "乌茲别克斯坦"],
        ["奥地利", "奧地利"],
        ["塞纳乔其", "塞那乔其"],
        ["国际友谊", "国陈友谊"],
    ]
    present = set(names)
    for g in known_groups:
        hit = [x for x in g if x in present]
        if len(hit) >= 2:
            notes.append(" / ".join(hit))
    return notes


def seed_entities(conn, *, dry_run: bool) -> dict:
    team_names = _distinct(conn, "home_team") + _distinct(conn, "away_team")
    # unique preserving order
    seen: set[str] = set()
    teams_raw: list[str] = []
    for n in team_names:
        if n not in seen:
            seen.add(n)
            teams_raw.append(n)
    comps_raw = _distinct(conn, "competition_name")

    created_teams = 0
    created_leagues = 0
    collisions: list[str] = []

    # 按规范化名分组：同一 norm 共用一个规范实体，raw 作 alias
    team_groups: dict[str, list[str]] = defaultdict(list)
    for raw in teams_raw:
        norm = normalize_name(raw)
        if not norm:
            continue
        team_groups[norm].append(raw)

    league_groups: dict[str, list[str]] = defaultdict(list)
    for raw in comps_raw:
        norm = normalize_name(raw)
        if not norm:
            continue
        league_groups[norm].append(raw)

    if dry_run:
        return {
            "team_groups": len(team_groups),
            "league_groups": len(league_groups),
            "team_raw": len(teams_raw),
            "league_raw": len(comps_raw),
            "near_dup_teams": _near_dup_notes(teams_raw),
            "near_dup_leagues": _near_dup_notes(comps_raw),
            "created_teams": 0,
            "created_leagues": 0,
            "collisions": [],
        }

    before_t = int(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0])
    before_l = int(conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0])

    for norm, raws in sorted(team_groups.items()):
        tid = resolve_or_create_team(conn, norm, source="seed_from_matches", create=True)
        if tid is None:
            collisions.append(f"team unresolved: {norm!r} raws={raws}")
            continue
        for raw in raws:
            if raw == norm:
                continue
            # 额外 alias；撞 UNIQUE 且指向他队则记冲突
            existing = conn.execute(
                "SELECT team_id FROM team_aliases WHERE alias = ?", (raw,)
            ).fetchone()
            if existing and int(existing[0]) != tid:
                collisions.append(
                    f"team alias collision: {raw!r} → existing team_id={existing[0]}, wanted {tid} ({norm})"
                )
                continue
            if not existing:
                try:
                    conn.execute(
                        "INSERT INTO team_aliases (alias, team_id, source) VALUES (?, ?, ?)",
                        (raw, tid, "seed_from_matches"),
                    )
                except Exception as e:  # noqa: BLE001
                    collisions.append(f"team alias insert fail {raw!r}: {e}")

    for norm, raws in sorted(league_groups.items()):
        lid = resolve_or_create_league(conn, norm, source="seed_from_matches", create=True)
        if lid is None:
            collisions.append(f"league unresolved: {norm!r} raws={raws}")
            continue
        for raw in raws:
            if raw == norm:
                continue
            existing = conn.execute(
                "SELECT league_id FROM league_aliases WHERE alias = ?", (raw,)
            ).fetchone()
            if existing and int(existing[0]) != lid:
                collisions.append(
                    f"league alias collision: {raw!r} → existing league_id={existing[0]}, wanted {lid} ({norm})"
                )
                continue
            if not existing:
                try:
                    conn.execute(
                        "INSERT INTO league_aliases (alias, league_id, source) VALUES (?, ?, ?)",
                        (raw, lid, "seed_from_matches"),
                    )
                except Exception as e:  # noqa: BLE001
                    collisions.append(f"league alias insert fail {raw!r}: {e}")

    after_t = int(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0])
    after_l = int(conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0])
    created_teams = after_t - before_t
    created_leagues = after_l - before_l

    return {
        "team_groups": len(team_groups),
        "league_groups": len(league_groups),
        "team_raw": len(teams_raw),
        "league_raw": len(comps_raw),
        "near_dup_teams": _near_dup_notes(teams_raw),
        "near_dup_leagues": _near_dup_notes(comps_raw),
        "created_teams": created_teams,
        "created_leagues": created_leagues,
        "collisions": collisions,
        "teams_raw_list": teams_raw,
        "comps_raw_list": comps_raw,
    }


def backfill_match_ids(conn, *, dry_run: bool) -> dict[str, int]:
    """按文本精确匹配 alias/canonical 回填 matches.*_id。"""
    rows = conn.execute(
        "SELECT id, home_team, away_team, competition_name FROM matches"
    ).fetchall()
    updated = 0
    home_ok = away_ok = league_ok = 0
    for r in rows:
        mid = int(r["id"])
        hid = resolve_or_create_team(
            conn, r["home_team"], source="seed_from_matches", create=False
        )
        aid = resolve_or_create_team(
            conn, r["away_team"], source="seed_from_matches", create=False
        )
        lid = resolve_or_create_league(
            conn, r["competition_name"], source="seed_from_matches", create=False
        )
        if hid:
            home_ok += 1
        if aid:
            away_ok += 1
        if lid:
            league_ok += 1
        if dry_run:
            continue
        conn.execute(
            """
            UPDATE matches
            SET home_team_id = ?, away_team_id = ?, league_id = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (hid, aid, lid, mid),
        )
        updated += 1
    return {
        "rows": len(rows),
        "updated": updated,
        "home_ok": home_ok,
        "away_ok": away_ok,
        "league_ok": league_ok,
    }


def write_review(stats: dict, collisions: list[str]) -> None:
    lines = [
        "# Alias 种子 · needs_review（2026-10-06）",
        "",
        "> 种子脚本**不强制合并**近重复；以下供人工择规范名后补 alias。",
        "",
        "## 近重复队名（繁简 / 错字）",
        "",
    ]
    for g in stats.get("near_dup_teams") or []:
        lines.append(f"- {g}")
    if not stats.get("near_dup_teams"):
        lines.append("- （无）")
    lines += ["", "## 近重复联赛名", ""]
    for g in stats.get("near_dup_leagues") or []:
        lines.append(f"- {g}")
    if not stats.get("near_dup_leagues"):
        lines.append("- （无）")
    lines += ["", "## UNIQUE(alias) 冲突", ""]
    if collisions:
        for c in collisions:
            lines.append(f"- `{c}`")
    else:
        lines.append("- （无）")
    lines += [
        "",
        "## 说明",
        "",
        "- 播种来源：`matches` DISTINCT `home_team` / `away_team` / `competition_name`",
        "- alias.source = `seed_from_matches`",
        "- 后续导入钩子 source = `import`",
        "",
    ]
    REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--db", type=Path, default=None, help="SQLite 路径（默认 app.db）")
    args = ap.parse_args()

    db_path = args.db or DB_PATH
    conn = connect(db_path)
    try:
        before = _counts(conn)
        print("BEFORE", before)
        stats = seed_entities(conn, dry_run=args.dry_run)
        print("SEED", {k: v for k, v in stats.items() if k not in ("teams_raw_list", "comps_raw_list")})
        bf = backfill_match_ids(conn, dry_run=args.dry_run)
        print("BACKFILL", bf)
        if not args.dry_run:
            conn.commit()
            write_review(stats, stats.get("collisions") or [])
            print(f"review → {REVIEW_PATH}")
        after = _counts(conn) if not args.dry_run else before
        if not args.dry_run:
            print("AFTER", after)
        else:
            print("(dry-run: no write)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
