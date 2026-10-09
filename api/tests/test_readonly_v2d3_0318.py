"""0.3.18：v2d3 只读实例（APP_DB_PATH / APP_DB_LABEL / APP_READONLY）。写接口 403，库 sha 不变，meta.db / promoted。"""
from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.db as adb  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

V2D3_DB = ROOT / "data" / "v2d3" / "app.db"
PROD_DB = ROOT / "data" / "app.db"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def ro_v2d3(monkeypatch):
    if not V2D3_DB.exists():
        pytest.skip("v2d3 replica missing")
    monkeypatch.setattr(adb, "DB_PATH", V2D3_DB)
    monkeypatch.setattr(adb, "DB_LABEL", "v2d3")
    monkeypatch.setattr(adb, "READONLY", True)
    return V2D3_DB


WRITES = [
    ("post", "/strategies/1/validate", {"scope": "all", "shadow": True}),
    ("post", "/strategies", {"strategy_key": "X", "version": "x"}),
    ("post", "/strategies/compose", {}),
    ("post", "/strategies/compare", {}),
    ("post", "/bankroll/snapshots", {}),
    ("post", "/bankroll/calc", {}),
    ("put", "/bankroll/config/x", {}),
    ("patch", "/strategies/1", {}),
    ("delete", "/strategies/1", None),
]


def test_readonly_rejects_all_writes_and_sha_unchanged(ro_v2d3):
    sha0 = _sha(ro_v2d3)
    c = TestClient(app)
    for method, path, body in WRITES:
        r = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        j = r.json()
        assert j["meta"] == {"db": "v2d3", "promoted": False, "readonly": True}
    # 读接口照常，带 meta
    h = c.get("/health").json()
    assert h["meta"] == {"db": "v2d3", "promoted": False, "readonly": True}
    t = c.get("/table/matches", params={"date_from": "2026-06-01", "date_to": "2026-06-02", "scope": "all"})
    assert t.status_code == 200 and t.json()["meta"]["db"] == "v2d3" and t.json()["meta"]["promoted"] is False
    assert _sha(ro_v2d3) == sha0


def test_readonly_connection_is_mode_ro(ro_v2d3):
    import sqlite3
    conn = adb.connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE _ro_probe (x INTEGER)")
    finally:
        conn.close()


def test_live_meta_default():
    h = TestClient(app).get("/health").json()
    assert h["meta"]["db"] == "live" and h["meta"]["promoted"] is True


def _import_db_with_env(env: dict) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if not k.startswith("APP_")}
    e.update(env)
    return subprocess.run([sys.executable, "-c", "import app.db as d; print(d.DB_LABEL, d.READONLY, d.DB_PATH)"],
                          cwd=str(ROOT), env=e, capture_output=True, text=True)


def test_env_parsing_and_guards():
    r = _import_db_with_env({"APP_DB_PATH": "data/v2d3/app.db", "APP_DB_LABEL": "v2d3", "APP_READONLY": "1"})
    assert r.returncode == 0 and r.stdout.split()[:2] == ["v2d3", "True"]
    assert r.stdout.strip().endswith("data/v2d3/app.db")
    r = _import_db_with_env({})
    assert r.returncode == 0 and r.stdout.split()[:2] == ["live", "False"]
    assert r.stdout.strip().endswith("data/app.db")
    # v2d3 必须只读；v2d3 不许指向现网库
    assert _import_db_with_env({"APP_DB_PATH": "data/v2d3/app.db", "APP_DB_LABEL": "v2d3"}).returncode != 0
    assert _import_db_with_env({"APP_DB_LABEL": "v2d3", "APP_READONLY": "1"}).returncode != 0
