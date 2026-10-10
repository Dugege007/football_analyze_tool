"""Opening quote status and source fields (user rule, 2026-10-10).

Every opening quote cell (China Sports Lottery Jingcai cells and Asian handicap cells) carries:

- status: "ok" (a real opening quote exists), "missing" (no real opening quote; the first value we
  captured ourselves must not replace it), or "suspect_truncated" (Asian handicap only: the first
  record from the 5DollarFootballAPI history is later than the 95th percentile of the external
  quantile table, so the history may be truncated).
- source_kind: "official_open" (the opening value comes from an interface or official opening data),
  "manual" (the opening value was recorded by hand by the user), or null when status is "missing".
  Manual and interface opening quotes are the same definition of the opening quote; source_kind only
  tells them apart so that backtests can count them separately.
- first_captured_at: the moment we first captured this book and market ourselves (null when never).
- open_time: the real opening time when it is known, otherwise null. Nothing is inferred.
- open_time_known: true only when open_time is not null.
- backtest_eligible: false when status is not "ok".
- truncation_checked, truncation_reason, truncation_group, first_record_lead_minutes, late_p95_lead_minutes:
  the result of the truncation check, flattened so that the flat table keeps a fixed column set.

The truncation check reads an external quantile table supplied by the football analyst
(default path: config/open_truncation_quantiles.json, can be overridden with the environment
variable OPEN_TRUNCATION_QUANTILES_PATH). This module never computes the table itself.
When the table file does not exist, no truncation check is made.

Table format (JSON):
{
  "min_samples": 30,
  "groups": [
    {"book": "macau", "league": "Premier League", "samples": 120, "late_p95_lead_minutes": 2880},
    {"book": "macau", "league": null, "samples": 900, "late_p95_lead_minutes": 3000}
  ]
}
late_p95_lead_minutes means: in that group, 95 percent of normal matches have their first record at
least this many minutes before kickoff. A match whose first record is fewer minutes before kickoff
than that value (that is, later than the 95th percentile of lateness) is suspect_truncated.
A group with league equal to null is the fallback group for the book. A book and league group
whose samples are fewer than min_samples is ignored and the book-only group is used instead.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_SUSPECT_TRUNCATED = "suspect_truncated"
SOURCE_OFFICIAL_OPEN = "official_open"
SOURCE_MANUAL = "manual"

DEFAULT_QUANTILE_PATH = Path(__file__).resolve().parents[2] / "config" / "open_truncation_quantiles.json"
DEFAULT_MIN_SAMPLES = 30

OPEN_STATUS_FIELDS = ("status", "source_kind", "first_captured_at", "open_time", "open_time_known",
                      "backtest_eligible", "truncation_checked", "truncation_reason", "truncation_group",
                      "first_record_lead_minutes", "late_p95_lead_minutes")


def quantile_table_path() -> Path:
    env = os.environ.get("OPEN_TRUNCATION_QUANTILES_PATH")
    return Path(env) if env else DEFAULT_QUANTILE_PATH


def load_quantile_table(path: Optional[Path] = None) -> Optional[dict]:
    """Return the table, or None when the file does not exist or cannot be parsed."""
    p = path or quantile_table_path()
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lookup_threshold(table: Optional[dict], book: str, league: Optional[str]) -> Optional[dict]:
    """Book and league group when it has enough samples, otherwise the book-only group, otherwise None."""
    if not table:
        return None
    min_n = int(table.get("min_samples") or DEFAULT_MIN_SAMPLES)
    groups = table.get("groups") or []
    if league:
        for g in groups:
            if g.get("book") == book and g.get("league") == league and int(g.get("samples") or 0) >= min_n:
                return {**g, "group": "book_league"}
    for g in groups:
        if g.get("book") == book and g.get("league") in (None, ""):
            return {**g, "group": "book_only"}
    return None


def truncation_check(table: Optional[dict], book: str, league: Optional[str],
                     first_record_at: Optional[datetime], kickoff: Optional[datetime]) -> dict:
    """Decide whether an Asian handicap history looks truncated. Never raises."""
    if table is None:
        return {"checked": False, "reason": "quantile_table_missing", "suspect": False}
    if first_record_at is None or kickoff is None:
        return {"checked": False, "reason": "first_record_or_kickoff_unknown", "suspect": False}
    g = lookup_threshold(table, book, league)
    if g is None or g.get("late_p95_lead_minutes") is None:
        return {"checked": False, "reason": "no_group_for_book", "suspect": False}
    lead = (kickoff - first_record_at).total_seconds() / 60.0
    thr = float(g["late_p95_lead_minutes"])
    return {"checked": True, "reason": None, "suspect": lead < thr, "group": g["group"],
            "first_record_lead_minutes": round(lead, 1), "late_p95_lead_minutes": thr}


def open_status(*, available: bool, source_kind: Optional[str], first_captured_at: Optional[str],
                open_time: Optional[str], truncation: Optional[dict] = None) -> dict:
    if not available:
        status, source_kind = STATUS_MISSING, None
    elif truncation and truncation.get("suspect"):
        status = STATUS_SUSPECT_TRUNCATED
    else:
        status = STATUS_OK
    t = truncation or {}
    return {"status": status, "source_kind": source_kind, "first_captured_at": first_captured_at,
            "open_time": open_time, "open_time_known": open_time is not None,
            "backtest_eligible": status == STATUS_OK,
            "truncation_checked": t.get("checked") if truncation is not None else None,
            "truncation_reason": t.get("reason"), "truncation_group": t.get("group"),
            "first_record_lead_minutes": t.get("first_record_lead_minutes"),
            "late_p95_lead_minutes": t.get("late_p95_lead_minutes")}


def blank_open_status() -> dict:
    return {k: None for k in OPEN_STATUS_FIELDS}
