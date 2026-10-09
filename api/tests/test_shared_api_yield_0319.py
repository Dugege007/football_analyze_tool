"""0.3.19 追加 2：补数让路判断。"""
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


import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import shared_api_yield as Y  # noqa: E402

CN = timezone(timedelta(hours=8))


def T(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 10, 9, h, m, tzinfo=CN)


@pytest.mark.parametrize("hm,want", [
    ("11:04", False), ("11:05", True), ("11:10", True), ("11:19", True), ("11:20", False),
    ("14:54", False), ("14:55", True), ("15:00", True), ("15:14", True), ("15:15", False),
    ("21:54", False), ("21:55", True), ("22:00", True), ("22:14", True), ("22:15", False),
    ("03:00", False), ("23:00", False),
])
def test_windows(hm, want):
    assert Y.must_yield(T(hm)) is want


def test_utc_input_converted():
    assert Y.must_yield(datetime(2026, 10, 9, 3, 10, tzinfo=timezone.utc))  # = 11:10 北京


def test_budget_constants():
    assert Y.SHARED_LIMIT_PER_MIN == 40 and Y.ANALYST_LIVE_MAX_PER_MIN == 24
    assert Y.BACKFILL_MAX_PER_MIN == 16 and Y.MIN_INTERVAL_SEC == 3.75


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start.timestamp()

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t, CN)


def _gate(tmp_path, start):
    fc = FakeClock(start)
    return Y.Gate("test", sleep=fc.sleep, clock=fc.clock, now=fc.now, ledger=tmp_path / "ledger.json"), fc


def test_gate_sleeps_through_window(tmp_path, monkeypatch):
    monkeypatch.setattr(Y, "LOG", tmp_path / "log.jsonl")
    g, fc = _gate(tmp_path, T("14:56"))
    g.before_request()
    assert fc.now() >= T("15:15") and not Y.must_yield(fc.now())
    assert g.stats["yield_sleeps"] == 1


def test_gate_rate_le_16_per_min(tmp_path, monkeypatch):
    monkeypatch.setattr(Y, "LOG", tmp_path / "log.jsonl")
    g, fc = _gate(tmp_path, T("02:00"))
    sent = []
    for _ in range(40):
        g.before_request()
        sent.append(fc.clock())
    for i in range(len(sent)):
        assert sum(1 for x in sent if sent[i] <= x < sent[i] + 60) <= 16


def test_two_processes_share_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(Y, "LOG", tmp_path / "log.jsonl")
    fc = FakeClock(T("02:00"))
    a = Y.Gate("a", sleep=fc.sleep, clock=fc.clock, now=fc.now, ledger=tmp_path / "l.json")
    b = Y.Gate("b", sleep=fc.sleep, clock=fc.clock, now=fc.now, ledger=tmp_path / "l.json")
    sent = []
    for i in range(32):
        (a if i % 2 else b).before_request()
        sent.append(fc.clock())
    assert all(sum(1 for x in sent if s <= x < s + 60) <= 16 for s in sent)


def test_quota_floor_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(Y, "LOG", tmp_path / "log.jsonl")
    g, fc = _gate(tmp_path, T("02:00"))
    g.after_response({"X-RateLimit-Remaining": "30"})
    assert g.pause_until == 0.0
    g.after_response({"X-RateLimit-Remaining": "24", "X-RateLimit-Reset": "20"})
    t0 = fc.clock()
    g.before_request()
    assert fc.clock() - t0 >= 21 and g.stats["quota_pauses"] == 1


@pytest.mark.parametrize("path", [
    "scripts/fill_1x2_odds_snap.py", "scripts/fill_macau_mid_water.py",
    str(_REPO_ROOT / "scripts/backfill/5df-multibook-history-queue/run_queue.py"),
    str(_REPO_ROOT / "scripts/backfill/5df-multibook-history-queue/map_csl_fixtures.py"),
    str(_REPO_ROOT / "scripts/backfill/5df-multibook-history-queue/map_alias90_fixtures.py"),
    str(_REPO_ROOT / "scripts/backfill/backfill_macau_mid_water.py"),
    str(_REPO_ROOT / "scripts/backfill/5dollar_odds_resume.py"),
])
def test_backfill_callers_wired(path):
    p = Path(path) if path.startswith("/") else ROOT / path
    src = p.read_text()
    assert "shared_api_yield" in src and ".before_request()" in src and ".after_response(" in src
