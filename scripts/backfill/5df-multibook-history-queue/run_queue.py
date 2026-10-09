#!/usr/bin/env python3
"""Worker: pull macauslot+pinnacle AH snap + history for pending fixtures.

Writes only under this queue dir (raw/ + logs/ + reports/). No prod DB.
Rate: ≤30/min (gap 2.1s). Stop if X-RateLimit-Remaining ≤ 5.
Checkpoint after each match. Full run (no --limit) requires CONFIRM_FULL_RUN=1.
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


import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 0.3.19：共享数据接口补数让路（北京 11:05–11:20 / 14:55–15:15 / 21:55–22:15 不发请求；补数合计 ≤16 次/分钟；
# 剩余 ≤24 停到 Reset）。共用判断：api/app/shared_api_yield.py
import sys as _yield_sys  # noqa: E402
_yield_sys.path.append(str(_MA_API_ROOT / "app"))  # 追加在末尾，不遮蔽其它模块
import shared_api_yield as _yield_mod  # noqa: E402
_YIELD = _yield_mod.Gate("run_queue")


def _gated_urlopen(req, timeout=60):
    _YIELD.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _YIELD.after_response(e.headers)
        raise
    _YIELD.after_response(r.headers)
    return r



ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
RAW_ODDS = ROOT / "raw" / "odds"
RAW_HIST = ROOT / "raw" / "hist"
LOG_DIR = ROOT / "logs"
REPORTS = ROOT / "reports"
BASE = "https://api.5dollarfootballapi.com/v1"
BOOKS = ["macauslot", "pinnacle"]
MARKET = "asian"
GAP_SEC = 2.1  # ≤30/min
RESERVE = 5
MAX_ERRORS = 5
TZ = timezone(timedelta(hours=8))
DUAL_WRITE_MARKER = ROOT / "DUAL_WRITE.off"
LEGACY_HIST = Path(
    str(_ODDS_DATA_DIR / "5dollar/macau-mid-backfill-2026-10-07/raw/hist")
)


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


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


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def assert_dual_write_off() -> None:
    if not DUAL_WRITE_MARKER.exists():
        raise SystemExit("Missing DUAL_WRITE.off marker — refusing to run")
    for k in ("ODDS_ASIAN_DUAL_WRITE", "DUAL_WRITE_ODDS_ASIAN", "DUAL_WRITE"):
        v = os.environ.get(k, "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            raise SystemExit(f"Env {k}={v!r} enabled — refuse (dual_write must stay OFF)")


def update_state(**kwargs) -> None:
    state_path = QUEUE / "state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state.update(kwargs)
    state["dual_write"] = "OFF"
    state["last_progress_at"] = now_iso()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class RateClient:
    def __init__(self, key: str | None, *, dry_run: bool):
        self.key = key
        self.dry_run = dry_run
        self.gap = GAP_SEC
        self.last = 0.0
        self.calls = 0
        self.remaining = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / "call_log.tsv"
        if not self.log_path.exists():
            self.log_path.write_text(
                "ts\tname\tpath\thttp\telapsed_s\tlimit\tremaining\treset\n",
                encoding="utf-8",
            )

    def stop_for_quota(self) -> bool:
        return self.remaining is not None and self.remaining <= RESERVE

    def get(self, name: str, path: str, params: dict | None = None):
        if self.dry_run:
            self.calls += 1
            return 200, {"success": 1, "data": {}, "_dry_run": True}, {}

        if not self.key or self.key.startswith("YOUR_"):
            raise SystemExit("missing FIVEDOLLAR_FOOTBALL_API_KEY")

        wait = self.gap - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)

        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
            },
        )
        t0 = time.time()
        try:
            r = _gated_urlopen(req, timeout=60)
            code, body, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, body, headers = e.code, e.read(), e.headers
        self.last = time.time()
        self.calls += 1
        rem = headers.get("X-RateLimit-Remaining")
        if rem is not None:
            try:
                self.remaining = int(rem)
            except ValueError:
                pass
        with self.log_path.open("a", encoding="utf-8") as f:
            # never log Authorization / key
            f.write(
                f"{datetime.now(TZ).strftime('%H:%M:%S')}\t{name}\t{path}\t{code}\t"
                f"{time.time()-t0:.2f}\t{headers.get('X-RateLimit-Limit')}\t{rem}\t"
                f"{headers.get('X-RateLimit-Reset')}\n"
            )
        try:
            data = json.loads(body)
        except Exception:
            data = {"_raw": body[:400].decode("utf-8", "ignore")}
        return code, data, headers


def pull_fixture(client: RateClient, row: dict, *, dry_run: bool) -> dict:
    fid = str(row["fixture_id"])
    report = {
        "fixture_id": fid,
        "match_uid": row.get("match_uid"),
        "jingcai_date": row.get("jingcai_date"),
        "at": now_iso(),
        "dry_run": dry_run,
        "calls": [],
        "ok": False,
        "error": None,
        "fatal": False,
    }
    RAW_ODDS.mkdir(parents=True, exist_ok=True)
    RAW_HIST.mkdir(parents=True, exist_ok=True)

    odds_path = RAW_ODDS / f"{fid}_macauslot_pinnacle.json"
    if odds_path.exists() and not dry_run:
        report["calls"].append({"name": "odds_snap", "status": "cache_hit"})
    else:
        if client.stop_for_quota():
            report["error"] = "rate_reserve"
            report["fatal"] = True
            return report
        code, data, _ = client.get(
            f"odds_{fid}",
            f"/fixtures/{fid}/odds",
            {"bookmakers": ",".join(BOOKS)},
        )
        entry = {"name": "odds_snap", "http": code}
        if dry_run:
            entry["planned"] = str(odds_path)
        report["calls"].append(entry)
        if not dry_run:
            if code == 200:
                odds_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                report["error"] = f"odds_http_{code}"
                report["fatal"] = code in (401, 403, 429)
                return report

    for book in BOOKS:
        hist_path = RAW_HIST / f"{fid}_{book}_{MARKET}.json"
        if hist_path.exists() and not dry_run:
            report["calls"].append({"name": f"hist_{book}", "status": "cache_hit"})
            continue
        legacy = LEGACY_HIST / f"{fid}_macauslot_asian.json"
        if (
            not dry_run
            and book == "macauslot"
            and legacy.exists()
            and not hist_path.exists()
        ):
            hist_path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            report["calls"].append({"name": f"hist_{book}", "status": "legacy_copy"})
            continue
        if client.stop_for_quota():
            report["error"] = "rate_reserve"
            report["fatal"] = True
            return report
        code, data, _ = client.get(
            f"hist_{fid}_{book}",
            f"/fixtures/{fid}/odds/history",
            {"bookmaker": book, "market": MARKET, "per_page": 500},
        )
        entry = {"name": f"hist_{book}", "http": code}
        if dry_run:
            entry["planned"] = str(hist_path)
        report["calls"].append(entry)
        if dry_run:
            continue
        if code == 200:
            hist_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            report["error"] = f"hist_{book}_http_{code}"
            report["fatal"] = code in (401, 403, 429)
            return report

    report["ok"] = True
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="5DF multibook history queue worker")
    ap.add_argument("--dry-run", action="store_true", help="Plan calls only; no API")
    ap.add_argument("--limit", type=int, default=None, help="Max fixtures this run")
    args = ap.parse_args()

    assert_dual_write_off()

    if args.limit is None and not args.dry_run:
        if os.environ.get("CONFIRM_FULL_RUN", "").strip() != "1":
            raise SystemExit(
                "Refusing full run: pass --limit N, or set CONFIRM_FULL_RUN=1"
            )

    pending = load_jsonl(QUEUE / "pending.jsonl")
    pending.sort(
        key=lambda r: (
            r.get("priority", 99),
            r.get("jingcai_date") or "",
            r.get("jc_id") or "",
        )
    )

    run = {
        "started_at": now_iso(),
        "dry_run": args.dry_run,
        "limit": args.limit,
        "pending_before": len(pending),
        "pulled_ok": 0,
        "errors": [],
        "stop": None,
        "api_calls": 0,
        "rate_remaining": None,
        "dual_write": "OFF",
    }

    if not pending:
        run["stop"] = "no_pending"
        print(json.dumps(run, ensure_ascii=False, indent=2))
        return 0

    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY")
    client = RateClient(key, dry_run=args.dry_run)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    fill_log = LOG_DIR / "fill_report.jsonl"

    processed = 0
    # Work on a mutable copy; dry-run never mutates pending file content permanently
    original_pending = list(pending)
    work = list(pending)

    while work:
        if args.limit is not None and processed >= args.limit:
            run["stop"] = "limit_reached"
            break
        if client.stop_for_quota():
            run["stop"] = "rate_reserve"
            break

        row = work.pop(0)
        rep = pull_fixture(client, row, dry_run=args.dry_run)
        append_jsonl(fill_log, rep)
        processed += 1
        run["api_calls"] = client.calls
        run["rate_remaining"] = client.remaining

        if args.dry_run:
            if rep.get("ok"):
                run["pulled_ok"] += 1
            else:
                run["errors"].append(
                    {"fixture_id": row.get("fixture_id"), "error": rep.get("error")}
                )
            continue

        # live: checkpoint after each match
        if rep.get("ok"):
            run["pulled_ok"] += 1
            done_row = {
                "fixture_id": row.get("fixture_id"),
                "match_uid": row.get("match_uid"),
                "jingcai_date": row.get("jingcai_date"),
                "jc_id": row.get("jc_id"),
                "home_team": row.get("home_team"),
                "away_team": row.get("away_team"),
                "books": row.get("books") or BOOKS,
                "market": row.get("market") or MARKET,
                "phase": row.get("phase"),
                "status": "done",
                "finished_at": now_iso(),
                "calls": rep.get("calls"),
            }
            append_jsonl(QUEUE / "done.jsonl", done_row)
            write_jsonl(QUEUE / "pending.jsonl", work)
            update_state(pending=len(work), done=len(load_jsonl(QUEUE / "done.jsonl")))
        else:
            # put back to front for retry later; keep rest
            work.insert(0, row)
            write_jsonl(QUEUE / "pending.jsonl", work)
            run["errors"].append(
                {"fixture_id": row.get("fixture_id"), "error": rep.get("error")}
            )
            update_state(pending=len(work), last_error=rep.get("error"))
            if rep.get("fatal"):
                run["stop"] = rep.get("error")
                break
            if len(run["errors"]) >= MAX_ERRORS:
                run["stop"] = "too_many_errors"
                break
            # non-fatal: skip this one for rest of run (move to end)
            failed = work.pop(0)
            work.append(failed)
            write_jsonl(QUEUE / "pending.jsonl", work)
    else:
        if run["stop"] is None:
            run["stop"] = "dry_run_complete" if args.dry_run else "pending_exhausted"

    if args.dry_run:
        write_jsonl(QUEUE / "pending.jsonl", original_pending)

    run["finished_at"] = now_iso()
    run["pending_after"] = len(load_jsonl(QUEUE / "pending.jsonl"))
    run["processed"] = processed
    out_path = REPORTS / f"run_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(run, ensure_ascii=False, indent=2))
    bad = run["stop"] in (
        "too_many_errors",
        "odds_http_401",
        "odds_http_403",
        "rate_reserve",
    ) and run["pulled_ok"] == 0
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
