#!/usr/bin/env python3
"""Build / refresh the 5DF multibook history pending queue (idempotent).

Sources (local only — no API):
  - $ODDS_DATA_DIR/backfill/macau_mid_water_fixture_map.json
  - CSL day caches under macau-mid-backfill / probe / this queue
  - sporttery monthly pages (JC year universe → unmapped if no fixture_id)

Writes under ./queue/ only. Never touches prod DB. Dual-write stays OFF.
"""
from __future__ import annotations
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


import csv
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
ODDS_DATA = Path(str(_ODDS_DATA_DIR))
FIXTURE_MAP = ODDS_DATA / "backfill" / "macau_mid_water_fixture_map.json"
SPORTTERY_CACHE = (
    ODDS_DATA / "research" / "jingcai-competition-whitelist" / "cache" / "sporttery"
)
CSL_DIRS = [
    ODDS_DATA / "5dollar" / "macau-mid-backfill-2026-10-07" / "raw" / "csl",
    ROOT / "raw" / "csl",
]
CSL_MAP_PATH = ROOT / "csl_fixture_map.json"
ALIAS90_MAP_PATH = ROOT / "csl_fixture_map_alias90.json"
WINDOW_START = "2025-10-01"
WINDOW_END = "2026-09-30"  # inclusive BJ calendar
CSL_NUMBER_FROM = "2025-10-25"  # 5DF CSL numbers roughly from mid-late Oct 2025
BOOKS = ["macauslot", "pinnacle"]
MARKET = "asian"
PHASE = "p1_macau_pinnacle_ah"
TZ = timezone(timedelta(hours=8))

WEEKDAY_FULL = {
    "一": "周一",
    "二": "周二",
    "三": "周三",
    "四": "周四",
    "五": "周五",
    "六": "周六",
    "日": "周日",
}
WEEKDAY_SHORT = {v: k for k, v in WEEKDAY_FULL.items()}


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def normalize_jc(number: str | None) -> str | None:
    if not number:
        return None
    s = str(number).strip()
    for full, short in WEEKDAY_SHORT.items():
        if s.startswith(full):
            return short + s[len(full) :]
    return s


def jc_full(short: str | None) -> str | None:
    if not short:
        return None
    ch = short[0]
    return (WEEKDAY_FULL.get(ch, ch) + short[1:]) if ch in WEEKDAY_FULL else short


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def in_window(date_s: str) -> bool:
    return WINDOW_START <= date_s <= WINDOW_END


def load_done_ids() -> set[str]:
    done = set()
    for r in load_jsonl(QUEUE / "done.jsonl"):
        fid = str(r.get("fixture_id") or "")
        if fid:
            done.add(fid)
    return done


def load_fixture_map_items() -> dict[str, dict]:
    """key = jingcai_date|short_jc → row with fixture_id"""
    out: dict[str, dict] = {}
    if not FIXTURE_MAP.exists():
        return out
    m = json.loads(FIXTURE_MAP.read_text(encoding="utf-8"))
    for it in m.get("items") or []:
        fid = it.get("fixture_id")
        jd = it.get("jingcai_date")
        short = normalize_jc(it.get("jc_id") or it.get("jc_norm"))
        if not fid or not jd or not short:
            continue
        if not in_window(jd):
            # still keep mapped June 2026 etc. — window filter applied to sporttery universe;
            # mapped items inside/near window are kept if jd in window OR always keep known maps
            pass
        key = f"{jd}|{short}"
        out[key] = {
            "fixture_id": str(fid),
            "jingcai_date": jd,
            "jc_id": short,
            "jc_norm": jc_full(short),
            "home_team": it.get("home_team"),
            "away_team": it.get("away_team"),
            "kickoff_at": it.get("kickoff_at") or it.get("fixture_ko"),
            "league_name": it.get("league_name"),
            "match_uid": it.get("match_uid") or f"{jd}|{short}",
            "source": "macau_mid_water_fixture_map",
        }
    return out


def load_csl_caches(mapped: dict[str, dict]) -> int:
    """Merge fixture ids from local CSL day JSON caches. Return newly added count."""
    added = 0
    date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
    for d in CSL_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            m = date_re.match(p.name)
            if not m:
                continue
            jd = m.group(1)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue
            for it in rows:
                lot = (it.get("lottery") or {}).get("jingcailottery") or {}
                short = normalize_jc(lot.get("number"))
                fid = it.get("id")
                if not short or not fid:
                    continue
                key = f"{jd}|{short}"
                if key in mapped:
                    continue
                teams = it.get("teams") or {}
                mapped[key] = {
                    "fixture_id": str(fid),
                    "jingcai_date": jd,
                    "jc_id": short,
                    "jc_norm": lot.get("number") or jc_full(short),
                    "home_team": (teams.get("home") or {}).get("name"),
                    "away_team": (teams.get("away") or {}).get("name"),
                    "kickoff_at": it.get("kickoff_utc"),
                    "league_name": (it.get("league") or {}).get("name"),
                    "match_uid": f"{jd}|{short}",
                    "source": f"csl_cache:{p.parent}",
                }
                added += 1
    return added




def load_csl_fixture_map(mapped: dict[str, dict]) -> int:
    """Merge items from csl_fixture_map.json (includes neighbor-joined sporttery keys)."""
    if not CSL_MAP_PATH.exists():
        return 0
    added = 0
    try:
        doc = json.loads(CSL_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for it in doc.get("items") or []:
        fid = it.get("fixture_id")
        jd = it.get("jingcai_date")
        short = normalize_jc(it.get("jc_id") or it.get("jc_norm"))
        if not fid or not jd or not short:
            continue
        key = f"{jd}|{short}"
        if key in mapped:
            # Prefer explicit neighbor-join / map entries over raw file-date cache
            src = it.get("source") or ""
            if src in ("csl_neighbor_join", "5df_chinasportslottery") or it.get("csl_file_date"):
                mapped[key] = {
                    "fixture_id": str(fid),
                    "jingcai_date": jd,
                    "jc_id": short,
                    "jc_norm": it.get("jc_norm") or jc_full(short),
                    "home_team": it.get("home_team"),
                    "away_team": it.get("away_team"),
                    "kickoff_at": it.get("kickoff_at") or it.get("fixture_ko"),
                    "league_name": it.get("league_name"),
                    "match_uid": it.get("match_uid") or key,
                    "source": src or "csl_fixture_map",
                    "csl_file_date": it.get("csl_file_date"),
                    "join_offset_days": it.get("join_offset_days"),
                }
            continue
        mapped[key] = {
            "fixture_id": str(fid),
            "jingcai_date": jd,
            "jc_id": short,
            "jc_norm": it.get("jc_norm") or jc_full(short),
            "home_team": it.get("home_team"),
            "away_team": it.get("away_team"),
            "kickoff_at": it.get("kickoff_at") or it.get("fixture_ko"),
            "league_name": it.get("league_name"),
            "match_uid": it.get("match_uid") or key,
            "source": it.get("source") or "csl_fixture_map",
            "csl_file_date": it.get("csl_file_date"),
            "join_offset_days": it.get("join_offset_days"),
        }
        added += 1
    return added



def load_alias90_fixture_map(mapped: dict[str, dict]) -> int:
    """Merge shadow alias90 map (team+≤90min window). Does not override existing keys."""
    if not ALIAS90_MAP_PATH.exists():
        return 0
    added = 0
    try:
        doc = json.loads(ALIAS90_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for it in doc.get("items") or []:
        fid = it.get("fixture_id")
        jd = it.get("jingcai_date")
        short = normalize_jc(it.get("jc_id") or it.get("jc_norm"))
        if not fid or not jd or not short:
            continue
        key = f"{jd}|{short}"
        if key in mapped and mapped[key].get("fixture_id"):
            continue  # never override CSL/macau/manual
        mapped[key] = {
            "fixture_id": str(fid),
            "jingcai_date": jd,
            "jc_id": short,
            "jc_norm": it.get("jc_norm") or jc_full(short),
            "home_team": it.get("home_team") or it.get("fixture_home"),
            "away_team": it.get("away_team") or it.get("fixture_away"),
            "kickoff_at": it.get("kickoff_at") or it.get("fixture_ko"),
            "league_name": it.get("league_name"),
            "match_uid": it.get("match_uid") or key,
            "source": "alias90_team_ko",
            "map_confidence": it.get("map_confidence") or "med",
            "kickoff_delta_min": it.get("kickoff_delta_min"),
            "rule_version": it.get("rule_version"),
        }
        added += 1
    return added


def neighbor_dates(jingcai_date: str) -> list[str]:
    base = datetime.strptime(jingcai_date, "%Y-%m-%d")
    return [(base + timedelta(days=off)).strftime("%Y-%m-%d") for off in (-1, 0, 1)]


def team_soft_score(u: dict, m: dict) -> int:
    uh = (u.get("home_team") or "").strip()
    ua = (u.get("away_team") or "").strip()
    ch = (m.get("home_team") or "").strip()
    ca = (m.get("away_team") or "").strip()
    score = 0
    if uh and ch and (uh in ch or ch in uh):
        score += 1
    if ua and ca and (ua in ca or ca in ua):
        score += 1
    return score


def lookup_mapped(u: dict, by_key: dict[str, dict]) -> dict | None:
    """Exact key, then ±1 day same jc_id (CSL day skew), with team tiebreak."""
    key = f"{u['jingcai_date']}|{u['jc_id']}"
    hit = by_key.get(key)
    if hit and hit.get("fixture_id"):
        return hit
    jd = u["jingcai_date"]
    jc = u["jc_id"]
    cands: list[dict] = []
    for nd in neighbor_dates(jd):
        if nd == jd:
            continue
        m = by_key.get(f"{nd}|{jc}")
        if m and m.get("fixture_id"):
            cands.append(m)
    # de-dupe by fixture_id
    seen: set[str] = set()
    uniq: list[dict] = []
    for m in cands:
        fid = str(m["fixture_id"])
        if fid in seen:
            continue
        seen.add(fid)
        uniq.append(m)
    if len(uniq) == 1:
        m = uniq[0]
        # Return a sporttery-keyed view so pending row carries sporttery date
        return {
            **m,
            "jingcai_date": jd,
            "jc_id": jc,
            "jc_norm": u.get("jc_norm") or m.get("jc_norm") or jc_full(jc),
            "match_uid": f"{jd}|{jc}",
            "source": (m.get("source") or "") + "+neighbor_lookup",
            "csl_file_date": m.get("csl_file_date") or m.get("jingcai_date"),
            "home_team": m.get("home_team") or u.get("home_team"),
            "away_team": m.get("away_team") or u.get("away_team"),
            "league_name": m.get("league_name") or u.get("league_name"),
        }
    if len(uniq) > 1:
        scored = sorted(((team_soft_score(u, m), m) for m in uniq), key=lambda t: -t[0])
        if scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            m = scored[0][1]
            return {
                **m,
                "jingcai_date": jd,
                "jc_id": jc,
                "jc_norm": u.get("jc_norm") or m.get("jc_norm") or jc_full(jc),
                "match_uid": f"{jd}|{jc}",
                "source": (m.get("source") or "") + "+neighbor_lookup_team",
                "csl_file_date": m.get("csl_file_date") or m.get("jingcai_date"),
                "home_team": m.get("home_team") or u.get("home_team"),
                "away_team": m.get("away_team") or u.get("away_team"),
                "league_name": m.get("league_name") or u.get("league_name"),
            }
    return None


def iter_sporttery_universe() -> list[dict]:
    """JC matches in WINDOW from sporttery monthly cache pages."""
    out = []
    if not SPORTTERY_CACHE.is_dir():
        return out
    # months 2025-10 .. 2026-09
    months = []
    for y in (2025, 2026):
        for mo in range(1, 13):
            ym = f"{y}-{mo:02d}"
            if ym < "2025-10" or ym > "2026-09":
                continue
            months.append(ym)
    for ym in months:
        for p in sorted(SPORTTERY_CACHE.glob(f"{ym}-p*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            val = d.get("value") or {}
            for mr in val.get("matchResult") or []:
                md = (mr.get("matchDate") or "")[:10]
                if not md or not in_window(md):
                    continue
                num = mr.get("matchNumStr") or mr.get("matchNum")
                short = normalize_jc(num)
                if not short:
                    continue
                out.append(
                    {
                        "jingcai_date": md,
                        "jc_id": short,
                        "jc_norm": jc_full(short) or num,
                        "home_team": mr.get("allHomeTeam") or mr.get("homeTeam"),
                        "away_team": mr.get("allAwayTeam") or mr.get("awayTeam"),
                        "league_name": mr.get("leagueName"),
                        "sporttery_match_id": mr.get("matchId"),
                        "match_uid": f"{md}|{short}",
                    }
                )
    # de-dupe by match_uid
    seen = set()
    uniq = []
    for r in out:
        k = r["match_uid"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def make_pending_row(mapped: dict, *, priority: int = 100) -> dict:
    return {
        "fixture_id": mapped["fixture_id"],
        "match_uid": mapped.get("match_uid"),
        "jingcai_date": mapped.get("jingcai_date"),
        "jc_id": mapped.get("jc_id"),
        "jc_norm": mapped.get("jc_norm"),
        "home_team": mapped.get("home_team"),
        "away_team": mapped.get("away_team"),
        "kickoff_at": mapped.get("kickoff_at"),
        "league_name": mapped.get("league_name"),
        "books": list(BOOKS),
        "market": MARKET,
        "phase": PHASE,
        "calls_planned": 3,
        "status": "pending",
        "priority": priority,
        "map_source": mapped.get("source"),
        "updated_at": now_iso(),
    }


def main() -> int:
    QUEUE.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_ids()
    mapped = load_fixture_map_items()
    csl_added = load_csl_caches(mapped)
    csl_map_added = load_csl_fixture_map(mapped)
    alias90_added = load_alias90_fixture_map(mapped)

    universe = iter_sporttery_universe()
    # Prefer CSL-numbered era for priority ranking
    pending_rows: list[dict] = []
    pending_fids: set[str] = set()
    mapped_in_universe = 0
    neighbor_joined = 0
    unmapped_rows: list[dict] = []

    # Index mapped by match_uid and by date|jc
    by_uid = {v.get("match_uid"): v for v in mapped.values() if v.get("match_uid")}
    by_key = mapped  # already jingcai_date|short

    for u in universe:
        key = f"{u['jingcai_date']}|{u['jc_id']}"
        m = by_key.get(key) or by_uid.get(u["match_uid"])
        if not (m and m.get("fixture_id")):
            m = lookup_mapped(u, by_key)
            if m:
                neighbor_joined += 1
        if m and m.get("fixture_id"):
            mapped_in_universe += 1
            fid = str(m["fixture_id"])
            if fid in done_ids or fid in pending_fids:
                continue
            # priority: earlier dates first within CSL era; pre-CSL lower
            pri = 10 if u["jingcai_date"] >= CSL_NUMBER_FROM else 50
            row = make_pending_row(m, priority=pri)
            # fill teams from sporttery if map missing names
            if not row.get("home_team"):
                row["home_team"] = u.get("home_team")
            if not row.get("away_team"):
                row["away_team"] = u.get("away_team")
            if not row.get("league_name"):
                row["league_name"] = u.get("league_name")
            pending_rows.append(row)
            pending_fids.add(fid)
        else:
            unmapped_rows.append(
                {
                    **u,
                    "reason": "no_5df_fixture_id",
                    "note": (
                        "await /chinasportslottery day map"
                        if u["jingcai_date"] >= CSL_NUMBER_FROM
                        else "pre_csl_number_era_or_unmapped"
                    ),
                    "updated_at": now_iso(),
                }
            )

    # Also enqueue mapped fixtures that fall outside sporttery universe pages
    # (e.g. map has June 2026 inside window but sporttery page incomplete) —
    # only if jingcai_date in window and not already pending/done.
    orphan_mapped = 0
    for key, m in mapped.items():
        jd = m.get("jingcai_date") or ""
        fid = str(m["fixture_id"])
        if fid in done_ids or fid in pending_fids:
            continue
        if jd and not in_window(jd):
            continue
        # if no jd, still include (known base)
        pri = 20 if (jd >= CSL_NUMBER_FROM if jd else True) else 60
        pending_rows.append(make_pending_row(m, priority=pri))
        pending_fids.add(fid)
        orphan_mapped += 1

    pending_rows.sort(
        key=lambda r: (r.get("priority", 99), r.get("jingcai_date") or "", r.get("jc_id") or "")
    )

    write_jsonl(QUEUE / "pending.jsonl", pending_rows)
    write_jsonl(QUEUE / "unmapped.jsonl", unmapped_rows)

    # Preserve done.jsonl as-is (create empty if missing)
    done_path = QUEUE / "done.jsonl"
    if not done_path.exists():
        done_path.write_text("", encoding="utf-8")

    state = {
        "built_at": now_iso(),
        "window": f"{WINDOW_START}..{WINDOW_END}",
        "csl_number_from": CSL_NUMBER_FROM,
        "books": BOOKS,
        "market": MARKET,
        "phase": PHASE,
        "sporttery_universe": len(universe),
        "mapped_total_local": len(mapped),
        "mapped_in_universe": mapped_in_universe,
        "csl_cache_added": csl_added,
        "csl_map_added": csl_map_added,
        "alias90_map_added": alias90_added,
        "neighbor_joined_in_universe": neighbor_joined,
        "orphan_mapped_enqueued": orphan_mapped,
        "pending": len(pending_rows),
        "done": len(done_ids),
        "unmapped": len(unmapped_rows),
        "dual_write": "OFF",
        "notes": [
            "Honest partial: pending ≈ locally mapped fixtures; year target ~5034.",
            "Unmapped rows need CSL day mapping or alias90 team+window map before pending.",
            "alias90 shadow: csl_fixture_map_alias90.json (med; no overwrite of CSL keys).",
            "Neighbor-day (±1) join applied against local CSL caches / csl_fixture_map (zero API).",
            "Idempotent: re-run merges; skips fixture_ids already in done.jsonl.",
        ],
    }
    (QUEUE / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # human-readable summary
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
