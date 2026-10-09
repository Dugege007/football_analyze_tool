"""公开仓：部分回归测试需要作者本机的真实库（api/data/app.db、api/data/v2d3/app.db）或私有回归库
（api/tests/fixtures/regress_db/*.db），这些文件不进公开仓。

规则：
1. 真实库缺失、或 api/data/app.db 只是 seed 演示库时，`_private_db_tests.txt` 中列出的测试在收集阶段即标记 skipped；
2. 兜底：任何测试若因「打不开 .db 文件」失败，且真实库缺失，同样改记 skipped（附原因）；
3. 调好的策略参数不随公开仓发布：测试若因 StrategyParamsMissing 失败（config/strategy_params.json 未填），
   改记 skipped，原因 "strategy params not configured"（见 docs/STRATEGY_PARAMS.md）。
其余失败（断言错误等）照常报错。把你自己的库放到上述路径后，这些测试会正常执行。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

# 测试不读取仓库根 .env（避免本机 APP_READONLY / APP_DB_PATH 等影响测试结果）；子进程同样继承此开关
os.environ["FAT_DISABLE_DOTENV"] = "1"

_HERE = Path(__file__).resolve().parent
_REPO = next(p for p in (_HERE, *_HERE.parents) if (p / "config.example.env").exists())
API_ROOT = _REPO / "api"
_APP_DB = API_ROOT / "data" / "app.db"
_V2D3_DB = API_ROOT / "data" / "v2d3" / "app.db"
_SKIP_REASON = "需要私有真实库/回归库（公开仓不含，见 docs/SENSITIVE.md）"
_PARAMS_SKIP_REASON = "strategy params not configured（调好的参数不随公开仓发布，见 docs/STRATEGY_PARAMS.md）"
_DEMO_MAX_MATCHES = 50  # seed 演示库只有 2 场


def _real_db_ready() -> bool:
    if not (_APP_DB.exists() and _V2D3_DB.exists()):
        return False
    try:
        conn = sqlite3.connect(f"file:{_APP_DB}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return n > _DEMO_MAX_MATCHES


_READY = _real_db_ready()
# 依赖私有回归库/基线的测试文件：回归库不在时即使有真实库也跳过
_FIXTURES = API_ROOT / "tests" / "fixtures"
_FIXTURE_FILES = {"test_not_evaluable.py", "regress_validate_capture.py"}
_FIXTURES_READY = (_FIXTURES / "regress_db" / "prod.db").exists() and (
    _FIXTURES / "validate_baseline_pre_not_evaluable.json").exists()
_LIST = _HERE / "_private_db_tests.txt"
# 依赖由真实数据生成的配置（api/config/kickoff_drift.json）的测试：公开仓只带空模板，真实文件不在时跳过
_CFG_SKIP_REASON = "需要由自己数据生成的 api/config/kickoff_drift.json（公开仓只带空模板，见 README「由你自己的数据生成的配置」）"
_CFG_READY = (API_ROOT / "config" / "kickoff_drift.json").exists()
_CFG_LIST = _HERE / "_private_cfg_tests.txt"
_CFG_KEYS = {
    ln.strip() for ln in (_CFG_LIST.read_text(encoding="utf-8").splitlines() if _CFG_LIST.exists() else [])
    if ln.strip() and not ln.startswith("#")
}
_KEYS = {
    ln.strip() for ln in (_LIST.read_text(encoding="utf-8").splitlines() if _LIST.exists() else [])
    if ln.strip() and not ln.startswith("#")
}


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.skip(reason=_SKIP_REASON)
    cfg_mark = pytest.mark.skip(reason=_CFG_SKIP_REASON)
    for item in items:
        key = f"{item.path.name}::{item.name}"
        if key in _CFG_KEYS and not _CFG_READY:
            item.add_marker(cfg_mark)
            continue
        if key not in _KEYS:
            continue
        if not _READY or (item.path.name in _FIXTURE_FILES and not _FIXTURES_READY):
            item.add_marker(mark)


def _is_missing_db_error(excinfo) -> bool:
    if excinfo is None:
        return False
    if excinfo.errisinstance(sqlite3.OperationalError) and "unable to open database file" in str(excinfo.value):
        return True
    if excinfo.errisinstance(FileNotFoundError):
        return str(getattr(excinfo.value, "filename", "") or "").endswith(".db")
    return False


def _is_params_missing(excinfo) -> bool:
    if excinfo is None:
        return False
    e = excinfo.value
    seen = 0
    while e is not None and seen < 10:
        if type(e).__name__ == "StrategyParamsMissing" or "StrategyParamsMissing" in str(e):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if not _READY and rep.failed and _is_missing_db_error(call.excinfo):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {_SKIP_REASON}")
    elif rep.failed and _is_params_missing(call.excinfo):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {_PARAMS_SKIP_REASON}")
