#!/usr/bin/env python3
"""Health check for 5DF multibook history queue.

Prints pending/done/error rates, last progress mtime, dual_write marker, ratelimit note.
Exit 0 when scaffold is healthy (pending>0 OR honest explanation via state).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
LOG_DIR = ROOT / "logs"
DUAL_WRITE_MARKER = ROOT / "DUAL_WRITE.off"
TZ = timezone(timedelta(hours=8))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat()


def main() -> int:
    pending = load_jsonl(QUEUE / "pending.jsonl")
    done = load_jsonl(QUEUE / "done.jsonl")
    unmapped = load_jsonl(QUEUE / "unmapped.jsonl")
    fill = load_jsonl(LOG_DIR / "fill_report.jsonl")
    errors = [r for r in fill if r.get("error")]
    ok = [r for r in fill if r.get("ok")]
    err_rate = (len(errors) / len(fill)) if fill else 0.0

    state = {}
    if (QUEUE / "state.json").exists():
        try:
            state = json.loads((QUEUE / "state.json").read_text(encoding="utf-8"))
        except Exception:
            state = {"_error": "state.json unreadable"}

    dual_ok = DUAL_WRITE_MARKER.exists()
    env_dual = {
        k: os.environ.get(k)
        for k in ("ODDS_ASIAN_DUAL_WRITE", "DUAL_WRITE_ODDS_ASIAN", "DUAL_WRITE")
        if os.environ.get(k)
    }

    progress_candidates = [
        QUEUE / "pending.jsonl",
        QUEUE / "done.jsonl",
        QUEUE / "state.json",
        LOG_DIR / "fill_report.jsonl",
        LOG_DIR / "call_log.tsv",
    ]
    last_mtime = None
    last_path = None
    for p in progress_candidates:
        if p.exists():
            mt = p.stat().st_mtime
            if last_mtime is None or mt > last_mtime:
                last_mtime = mt
                last_path = str(p)

    report = {
        "at": datetime.now(TZ).isoformat(),
        "pending": len(pending),
        "done": len(done),
        "unmapped": len(unmapped),
        "fill_ok": len(ok),
        "fill_errors": len(errors),
        "error_rate": round(err_rate, 4),
        "last_progress_mtime": (
            datetime.fromtimestamp(last_mtime, TZ).isoformat() if last_mtime else None
        ),
        "last_progress_path": last_path,
        "dual_write_marker": str(DUAL_WRITE_MARKER),
        "dual_write_marker_present": dual_ok,
        "dual_write_env": env_dual or None,
        "ratelimit_note": (
            "Self-cap ≤30/min (gap 2.1s); stop when X-RateLimit-Remaining≤5."
        ),
        "state_summary": {
            k: state.get(k)
            for k in (
                "built_at",
                "window",
                "sporttery_universe",
                "mapped_total_local",
                "pending",
                "done",
                "unmapped",
                "books",
                "phase",
                "notes",
            )
            if k in state
        },
        "ok": False,
        "message": "",
    }

    if not dual_ok:
        report["message"] = "DUAL_WRITE.off missing"
        report["ok"] = False
    elif env_dual and any(
        str(v).strip().lower() in ("1", "true", "yes", "on") for v in env_dual.values()
    ):
        report["message"] = "dual_write env enabled — unsafe"
        report["ok"] = False
    elif len(pending) > 0:
        report["ok"] = True
        report["message"] = f"pending={len(pending)} ready for P1"
    elif state.get("sporttery_universe") and state.get("unmapped", 0) > 0:
        report["ok"] = True
        report["message"] = (
            f"pending=0 but unmapped={state.get('unmapped')} "
            "(need CSL day mapping to grow pending); scaffold healthy"
        )
    elif len(done) > 0 and len(pending) == 0:
        report["ok"] = True
        report["message"] = "pending=0 and done>0 (queue drained)"
    else:
        report["ok"] = False
        report["message"] = "no pending and no explanation — run build_queue.py"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
