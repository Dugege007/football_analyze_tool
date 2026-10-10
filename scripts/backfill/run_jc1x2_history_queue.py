#!/usr/bin/env python3
"""Runner for the Jingcai win draw loss history backfill queue (analyst decision, 2026-10-10).

Commands:
  start   run the queue in the background (writes a pid file and a log); --foreground runs in this terminal.
  stop    ask the running runner to stop (stop file plus SIGTERM to the pid in the pid file).
  status  print the state file, the pid and whether that process is alive.

Rules:
- Running hours: Monday to Friday, 08:14 to 23:44 Beijing time. Outside these hours the runner sleeps until the
  next window opens (or exits when --exit-outside-window is given).
- Giving way: the three live yield windows of api/app/shared_api_yield.py and the busy checks of
  scripts/backfill/5df-multibook-history-queue/hist_yield.py (live due targets, live writes, rescue, low quota).
  Every request goes through shared_api_yield.Gate, so it shares the same quota ledger as every other backfill
  (all backfills together at most 16 requests per minute; pause when the remaining quota is 24 or less).
- Own rate: at most 6 requests per minute while the full-match backfill is running, at most 8 when it is not.
  The full-match backfill counts as running when a process whose command line contains run_fullmatch_queue.py
  exists (read from /proc) and its queue file queue/pending.jsonl still has lines. When neither holds (no process,
  or the queue is empty), it counts as stopped or finished.
- One request per match: GET /fixtures/{id}/odds/history?bookmaker=chinasportslottery&market=1x2. The raw response
  is saved as raw/hist/<id>_chinasportslottery_1x2.json and its SHA-256 checksum is recorded in the queue state.
- Resumable: the state file (state.json) records every finished, empty and failed match; a restart skips them.
  A failed request is retried up to 2 more times (3 attempts in total), each attempt is logged; after that the
  match goes to status failed and is not retried automatically.
- Automatic stop: 3 consecutive HTTP 401 or 403 responses, 5 consecutive 5xx responses or network errors, or a
  quota header that cannot be read or is below zero. The reason is written into the state file.
- Import: after every batch (default 20 newly downloaded files) the runner makes an online SQLite backup of the
  research copy and then imports the batch with api/scripts/import_jc_1x2_history_segments.py. Matches that are
  not in the research copy are not created; only their raw files are kept. The live database is never touched.

Full start command (do not run before the analyst's routine on Monday):
  cd /workspace/football-analyze-tool && .venv/bin/python scripts/backfill/run_jc1x2_history_queue.py start
Stop command:
  cd /workspace/football-analyze-tool && .venv/bin/python scripts/backfill/run_jc1x2_history_queue.py stop
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api" / "app"))
sys.path.insert(0, str(REPO / "api" / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "backfill" / "5df-multibook-history-queue"))

TZ = timezone(timedelta(hours=8))
ODDS_DATA = Path(os.environ.get("ODDS_DATA_DIR") or "/workspace/odds-data")
QDIR = Path(os.environ.get("JC1X2_QUEUE_DIR") or ODDS_DATA / "backfill" / "5df-jc1x2-history-queue")
FULLMATCH_DIR = ODDS_DATA / "backfill" / "5df-fullmatch-history-queue"
REPLICA_DB = Path(os.environ.get("V2D3_DB_PATH") or "/workspace/match-analysis-api/data/v2d3/app.db")
BACKUP_ROOT = Path(os.environ.get("JC1X2_BACKUP_ROOT") or "/workspace/backups")
BASE = "https://api.5dollarfootballapi.com/v1"
CALLER = "jc1x2_history_queue"

WINDOW_START = (8, 14)
WINDOW_END = (23, 44)
RATE_WITH_FULLMATCH = 6
RATE_ALONE = 8
MAX_ATTEMPTS = 3
STOP_AFTER_AUTH_ERRORS = 3
STOP_AFTER_SERVER_ERRORS = 5
BATCH_SIZE = 20


# ----------------------------------------------------------------------------- paths
def paths(qdir: Path = QDIR) -> dict[str, Path]:
    return {"pending": qdir / "queue" / "pending.jsonl", "state": qdir / "queue" / "state.json",
            "raw": qdir / "raw" / "hist", "log": qdir / "logs" / "runner.log", "pid": qdir / "runner.pid",
            "stop": qdir / "STOP"}


def now_cn() -> datetime:
    return datetime.now(TZ)


def log(p: dict, msg: str, **kw: Any) -> None:
    p["log"].parent.mkdir(parents=True, exist_ok=True)
    with p["log"].open("a", encoding="utf-8") as f:  # never logs the key
        f.write(json.dumps({"at": now_cn().isoformat(timespec="seconds"), "msg": msg, **kw}, ensure_ascii=False) + "\n")


def load_state(p: dict) -> dict:
    try:
        return json.loads(p["state"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"matches": {}, "status": "new", "stop_reason": None}


def save_state(p: dict, st: dict) -> None:
    p["state"].parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = now_cn().isoformat(timespec="seconds")
    tmp = p["state"].with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p["state"])


# ----------------------------------------------------------------------------- window and rate
def in_window(n: datetime) -> bool:
    if n.weekday() >= 5:
        return False
    a = n.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    b = n.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    return a <= n < b


def seconds_until_window(n: datetime) -> float:
    if in_window(n):
        return 0.0
    d = n
    for _ in range(8):
        a = d.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
        if d.weekday() < 5 and a > n:
            return (a - n).total_seconds()
        d = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return 3600.0


def fullmatch_running(proc_root: Path = Path("/proc"), fullmatch_dir: Path = FULLMATCH_DIR) -> bool:
    """Running = a process with run_fullmatch_queue.py in its command line exists and its pending queue has lines."""
    proc = False
    try:
        for d in proc_root.iterdir():
            if d.name.isdigit():
                try:
                    if b"run_fullmatch_queue.py" in (d / "cmdline").read_bytes():
                        proc = True
                        break
                except OSError:
                    continue
    except OSError:
        pass
    pend = fullmatch_dir / "queue" / "pending.jsonl"
    try:
        has_pending = any(line.strip() for line in pend.read_text(encoding="utf-8").splitlines())
    except OSError:
        has_pending = False
    return proc and has_pending


class OwnRate:
    """At most N requests in any 60 seconds for this queue (on top of the shared ledger)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.stamps: deque[float] = deque()

    def wait_seconds(self, limit: int) -> float:
        t = self.clock()
        while self.stamps and self.stamps[0] <= t - 60.0:
            self.stamps.popleft()
        if len(self.stamps) < limit:
            return 0.0
        return self.stamps[0] + 60.0 - t

    def record(self) -> None:
        self.stamps.append(self.clock())


# ----------------------------------------------------------------------------- fetch
def http_fetch(fid: str, gate) -> tuple[int, bytes, dict]:
    key = os.environ.get("FIVEDOLLAR_FOOTBALL_API_KEY", "")
    if not key or key.startswith("YOUR_"):
        raise SystemExit("FIVEDOLLAR_FOOTBALL_API_KEY is missing")
    url = f"{BASE}/fixtures/{fid}/odds/history?" + urllib.parse.urlencode(
        {"bookmaker": "chinasportslottery", "market": "1x2"})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    gate.before_request()
    try:
        r = urllib.request.urlopen(req, timeout=60)
        code, body, headers = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        code, body, headers = e.code, e.read(), dict(e.headers)
    gate.after_response(headers)
    return code, body, headers


def classify(code: int, body: bytes) -> str:
    if code == 200:
        try:
            ticks = ((json.loads(body).get("data") or {}).get("ticks")) or []
        except (ValueError, AttributeError):
            return "bad_body"
        return "done" if ticks else "empty"
    if code in (401, 403):
        return "auth_error"
    if code >= 500:
        return "server_error"
    return f"http_{code}"


# ----------------------------------------------------------------------------- import
def backup_replica(db: Path, root: Path) -> Path:
    d = root / f"v2d3-before-jc1x2-queue-import-{now_cn():%Y%m%dT%H%M%S}"
    d.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    t = sqlite3.connect(d / "app.db")
    s.backup(t)
    t.close()
    s.close()
    (d / "ROLLBACK.md").write_text(
        "# Rollback for one batch of the Jingcai win draw loss history queue\n\n"
        f"Backup of {db} taken before the batch import.\n\n"
        "Preferred rollback (keeps later writes): delete the imported rows of this batch by source and match.\n\n"
        f"    /workspace/football-analyze-tool/.venv/bin/python -c \"import sqlite3; c=sqlite3.connect('{db}', "
        "timeout=60); n=c.execute(\\\"DELETE FROM odds_timeline_seg WHERE source='5df_hist_jc_1x2'\\\").rowcount; "
        "c.commit(); print(n)\"\n\nThe command above removes every row of the Jingcai history import; restore the "
        "full backup only with the analyst's approval and after stopping all writers:\n\n"
        f"    /workspace/football-analyze-tool/.venv/bin/python -c \"import sqlite3; s=sqlite3.connect('{d / 'app.db'}');"
        f" t=sqlite3.connect('{db}', timeout=60); s.backup(t); t.close()\"\n", encoding="utf-8")
    return d


def import_batch(files: list[Path], db: Path, backup_root: Path) -> dict:
    """Online backup, then import the files that map to matches already in the research copy."""
    import import_jc_1x2_history_segments as jc
    if db.name != "app.db" or db.parent.name != "v2d3":
        raise SystemExit(f"Refusing to write {db}: only the v2d3 research copy is allowed.")
    bdir = backup_replica(db, backup_root)
    conn = sqlite3.connect(str(db), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    mapping = jc.build_mapping(conn)
    conn.execute("BEGIN IMMEDIATE")
    res = [jc.import_file(conn, f, mapping) for f in files]
    conn.commit()
    conn.close()
    return {"backup": str(bdir), "files": len(files), "segments": sum(r["segments"] for r in res),
            "imported": sum(1 for r in res if r.get("segments")),
            "not_in_replica": [r["fixture_id"] for r in res if r["status"] == "skipped_unmapped"]}


# ----------------------------------------------------------------------------- main loop
def run(p: dict, *, fetch: Optional[Callable[[str], tuple[int, bytes, dict]]] = None,
        gate=None, sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = now_cn,
        busy: Optional[Callable[[datetime], Optional[str]]] = None,
        fullmatch: Callable[[], bool] = fullmatch_running, importer: Callable[[list[Path]], dict] | None = None,
        exit_outside_window: bool = False, max_requests: Optional[int] = None, batch_size: int = BATCH_SIZE) -> dict:
    if gate is None:
        import shared_api_yield as Y
        gate = Y.Gate(CALLER)
    if fetch is None:
        fetch = lambda fid: http_fetch(fid, gate)  # noqa: E731
    if busy is None:
        try:
            import hist_yield as hy
            busy = lambda n: hy.check_yield_reason(n, check_recent_write=True)  # noqa: E731
        except Exception:  # noqa: BLE001
            busy = lambda n: None  # noqa: E731
    if importer is None:
        importer = lambda fs: import_batch(fs, REPLICA_DB, BACKUP_ROOT)  # noqa: E731
    st = load_state(p)
    st.update({"status": "running", "stop_reason": None, "pid": os.getpid()})
    save_state(p, st)
    queue = [json.loads(x) for x in p["pending"].read_text(encoding="utf-8").splitlines() if x.strip()]
    rate = OwnRate()
    auth_err = srv_err = requests = 0
    batch: list[Path] = []

    def flush():
        nonlocal batch
        if batch:
            r = importer(batch)
            st.setdefault("imports", []).append({"at": now().isoformat(timespec="seconds"), **r})
            log(p, "batch_imported", **{k: v for k, v in r.items() if k != "not_in_replica"},
                not_in_replica=len(r.get("not_in_replica") or []))
            batch = []
            save_state(p, st)

    def stop(reason: str) -> dict:
        flush()
        st.update({"status": "stopped", "stop_reason": reason})
        save_state(p, st)
        log(p, "stopped", reason=reason)
        return st

    todo = deque(queue)
    while todo:
        item = todo.popleft()
        fid = str(item["fixture_id"])
        m = st["matches"].get(fid) or {}
        if m.get("status") in ("done", "empty", "failed"):
            continue
        while True:
            if p["stop"].exists():
                return stop("stop_requested")
            n = now()
            if not in_window(n):
                if exit_outside_window:
                    return stop("outside_window")
                flush()
                st["status"] = "sleeping_outside_window"
                save_state(p, st)
                sleep(min(seconds_until_window(n), 900.0))
                continue
            reason = busy(n)
            if reason:
                st["status"] = f"yielding:{reason}"
                save_state(p, st)
                sleep(60.0)
                continue
            limit = RATE_WITH_FULLMATCH if fullmatch() else RATE_ALONE
            w = rate.wait_seconds(limit)
            if w > 0:
                sleep(w)
                continue
            break
        if max_requests is not None and requests >= max_requests:
            return stop("max_requests_reached")
        rate.record()
        requests += 1
        attempts = int(m.get("attempts") or 0) + 1
        try:
            code, body, headers = fetch(fid)
        except (urllib.error.URLError, OSError) as e:
            code, body, headers = 599, str(e).encode(), {}
        kind = classify(code, body)
        rem = headers.get("X-RateLimit-Remaining") if headers else None
        if rem is not None:
            try:
                if int(rem) < 0:
                    return stop(f"quota_abnormal:remaining={rem}")
            except ValueError:
                return stop(f"quota_abnormal:remaining={rem!r}")
        rec = {"status": kind, "http": code, "attempts": attempts, "at": now().isoformat(timespec="seconds"),
               "match_uid": item.get("match_uid")}
        if kind in ("done", "empty"):
            p["raw"].mkdir(parents=True, exist_ok=True)
            f = p["raw"] / f"{fid}_chinasportslottery_1x2.json"
            f.write_bytes(body)
            rec.update({"raw_path": str(f), "sha256": hashlib.sha256(body).hexdigest()})
            auth_err = srv_err = 0
            if kind == "done":
                batch.append(f)
        else:
            if kind == "auth_error":
                auth_err += 1
            elif kind == "server_error":
                srv_err += 1
            rec["status"] = "failed" if attempts >= MAX_ATTEMPTS else "retry"
            rec["error"] = kind
        st["matches"][fid] = rec
        save_state(p, st)
        if rec["status"] == "retry":
            todo.appendleft(item)  # retried right away, still through every gate
        log(p, "fetched", fixture_id=fid, http=code, result=rec["status"], attempts=attempts)
        if auth_err >= STOP_AFTER_AUTH_ERRORS:
            return stop(f"consecutive_auth_errors:{auth_err}")
        if srv_err >= STOP_AFTER_SERVER_ERRORS:
            return stop(f"consecutive_server_errors:{srv_err}")
        if len(batch) >= batch_size:
            flush()
    # retries left in this pass are tried again on the next start
    st_left = [k for k, v in st["matches"].items() if v.get("status") == "retry"]
    return stop("queue_finished" if not st_left else f"pass_finished_retries_pending:{len(st_left)}")


# ----------------------------------------------------------------------------- commands
def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_start(p: dict, a) -> int:
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text().strip())
            if pid_alive(pid):
                print(f"already running, pid {pid}")
                return 1
        except ValueError:
            pass
    p["stop"].unlink(missing_ok=True)
    if not a.foreground:
        p["log"].parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, str(Path(__file__).resolve()), "start", "--foreground"]
        if a.exit_outside_window:
            args.append("--exit-outside-window")
        out = (p["log"].parent / "runner.stdout.log").open("a")
        proc = subprocess.Popen(args, stdout=out, stderr=out, start_new_session=True, cwd=str(REPO))
        print(f"started, pid {proc.pid}; log {p['log']}")
        return 0
    p["pid"].write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: p["stop"].touch())
    try:
        st = run(p, exit_outside_window=a.exit_outside_window)
        print(json.dumps({"status": st["status"], "stop_reason": st["stop_reason"]}, ensure_ascii=False))
    finally:
        p["pid"].unlink(missing_ok=True)
    return 0


def cmd_stop(p: dict, a) -> int:
    p["stop"].parent.mkdir(parents=True, exist_ok=True)
    p["stop"].touch()
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text().strip())
            if pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                print(f"stop requested, pid {pid}")
                return 0
        except ValueError:
            pass
    print("stop file written; no running runner found")
    return 0


def cmd_status(p: dict, a) -> int:
    st = load_state(p)
    pid = None
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text().strip())
        except ValueError:
            pid = None
    counts: dict[str, int] = {}
    for v in st.get("matches", {}).values():
        counts[v.get("status")] = counts.get(v.get("status"), 0) + 1
    total = sum(1 for x in p["pending"].read_text(encoding="utf-8").splitlines() if x.strip()) \
        if p["pending"].exists() else 0
    print(json.dumps({"status": st.get("status"), "stop_reason": st.get("stop_reason"), "pid": pid,
                      "alive": bool(pid and pid_alive(pid)), "queue_total": total, "counts": counts,
                      "updated_at": st.get("updated_at"), "imports": len(st.get("imports") or [])},
                     ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["start", "stop", "status"])
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--exit-outside-window", action="store_true")
    a = ap.parse_args(argv)
    p = paths()
    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[a.command](p, a)


if __name__ == "__main__":
    raise SystemExit(main())
