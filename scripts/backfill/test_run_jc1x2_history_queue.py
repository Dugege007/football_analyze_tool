"""Tests for the Jingcai win draw loss history queue runner. No interface is called."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_jc1x2_history_queue as r  # noqa: E402

TZ = timezone(timedelta(hours=8))
MON = datetime(2026, 10, 12, 10, 0, tzinfo=TZ)


class _Gate:
    def before_request(self):
        pass

    def after_response(self, h):
        pass


def _setup(tmp_path, n=3):
    p = r.paths(tmp_path)
    p["pending"].parent.mkdir(parents=True)
    p["pending"].write_text("".join(json.dumps({"fixture_id": str(i), "match_uid": f"u{i}"}) + "\n"
                                    for i in range(1, n + 1)), encoding="utf-8")
    return p


def _body(ticks=1):
    return json.dumps({"data": {"ticks": [{"home": 2.0, "draw": 3.0, "away": 4.0,
                                           "recorded_at": "2026-10-10T00:00:00+00:00"}] * ticks}}).encode()


def test_window():
    assert r.in_window(MON)
    assert not r.in_window(MON.replace(hour=8, minute=13))
    assert r.in_window(MON.replace(hour=8, minute=14))
    assert not r.in_window(MON.replace(hour=23, minute=44))
    assert not r.in_window(datetime(2026, 10, 10, 12, 0, tzinfo=TZ))  # Saturday
    assert r.seconds_until_window(datetime(2026, 10, 10, 12, 0, tzinfo=TZ)) == (
        datetime(2026, 10, 12, 8, 14, tzinfo=TZ) - datetime(2026, 10, 10, 12, 0, tzinfo=TZ)).total_seconds()


def test_own_rate():
    t = [0.0]
    o = r.OwnRate(clock=lambda: t[0])
    for _ in range(6):
        assert o.wait_seconds(6) == 0
        o.record()
    assert o.wait_seconds(6) == 60.0 and o.wait_seconds(8) == 0
    t[0] = 61.0
    assert o.wait_seconds(6) == 0


def test_fullmatch_detection(tmp_path):
    proc = tmp_path / "proc"
    (proc / "12").mkdir(parents=True)
    (proc / "12" / "cmdline").write_bytes(b"python\x00run_fullmatch_queue.py\x00")
    fm = tmp_path / "fm"
    (fm / "queue").mkdir(parents=True)
    (fm / "queue" / "pending.jsonl").write_text('{"a":1}\n')
    assert r.fullmatch_running(proc, fm)
    (fm / "queue" / "pending.jsonl").write_text("")
    assert not r.fullmatch_running(proc, fm)


def test_run_saves_raw_resumes_and_imports(tmp_path):
    p = _setup(tmp_path)
    calls, imported = [], []
    def fetch(fid):
        calls.append(fid)
        return 200, _body(1 if fid != "2" else 0), {"X-RateLimit-Remaining": "100"}
    st = r.run(p, fetch=fetch, gate=_Gate(), sleep=lambda s: None, now=lambda: MON, busy=lambda n: None,
               fullmatch=lambda: False, importer=lambda fs: imported.append(list(fs)) or {"files": len(fs),
                                                                                    "segments": 1, "imported": 1},
               batch_size=10)
    assert st["stop_reason"] == "queue_finished" and calls == ["1", "2", "3"]
    assert st["matches"]["2"]["status"] == "empty" and st["matches"]["1"]["sha256"]
    assert (p["raw"] / "1_chinasportslottery_1x2.json").exists()
    assert [f.name.split("_")[0] for f in imported[0]] == ["1", "3"]
    calls.clear()
    r.run(p, fetch=fetch, gate=_Gate(), sleep=lambda s: None, now=lambda: MON, busy=lambda n: None,
          fullmatch=lambda: False, importer=lambda fs: {"files": 0, "segments": 0, "imported": 0})
    assert calls == []  # resumed: nothing fetched again


def test_retry_then_failed_and_auth_stop(tmp_path):
    p = _setup(tmp_path, 2)
    st = r.run(p, fetch=lambda fid: (500, b"", {}), gate=_Gate(), sleep=lambda s: None, now=lambda: MON,
               busy=lambda n: None, fullmatch=lambda: True, importer=lambda fs: {})
    assert st["matches"]["1"]["status"] == "failed" and st["matches"]["1"]["attempts"] == 3
    assert st["stop_reason"] == "consecutive_server_errors:5"
    p2 = _setup(tmp_path / "b", 5)
    st = r.run(p2, fetch=lambda fid: (403, b"", {}), gate=_Gate(), sleep=lambda s: None, now=lambda: MON,
               busy=lambda n: None, fullmatch=lambda: True, importer=lambda fs: {})
    assert st["stop_reason"] == "consecutive_auth_errors:3" and st["status"] == "stopped"


def test_outside_window_and_stop_file(tmp_path):
    p = _setup(tmp_path)
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)
    st = r.run(p, fetch=lambda fid: 1 / 0, gate=_Gate(), sleep=lambda s: None, now=lambda: sat,
               busy=lambda n: None, exit_outside_window=True, importer=lambda fs: {})
    assert st["stop_reason"] == "outside_window"
    p["stop"].touch()
    st = r.run(p, fetch=lambda fid: 1 / 0, gate=_Gate(), sleep=lambda s: None, now=lambda: MON,
               busy=lambda n: None, importer=lambda fs: {})
    assert st["stop_reason"] == "stop_requested"


def test_quota_abnormal_stops(tmp_path):
    p = _setup(tmp_path)
    st = r.run(p, fetch=lambda fid: (200, _body(), {"X-RateLimit-Remaining": "abc"}), gate=_Gate(),
               sleep=lambda s: None, now=lambda: MON, busy=lambda n: None, fullmatch=lambda: False,
               importer=lambda fs: {})
    assert st["stop_reason"].startswith("quota_abnormal")


def test_yield_reason_sleeps_then_continues(tmp_path):
    p = _setup(tmp_path, 1)
    seq = ["live_due", None]
    slept = []
    st = r.run(p, fetch=lambda fid: (200, _body(), {}), gate=_Gate(), sleep=slept.append, now=lambda: MON,
               busy=lambda n: seq.pop(0) if seq else None, fullmatch=lambda: False,
               importer=lambda fs: {"files": len(fs), "segments": 0, "imported": 0})
    assert slept == [60.0] and st["stop_reason"] == "queue_finished"
