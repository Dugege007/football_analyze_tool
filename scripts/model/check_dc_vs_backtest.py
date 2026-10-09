"""0.3.17 item 6：dc_model.ah_cover_prob vs app.backtest quarter_split/settle_code 对账（只读，不接生成器）。"""
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

import json, sys
sys.path.insert(0, str(_REPO_ROOT / "scripts/live")); sys.path.insert(0, str(_MA_API_ROOT))
import numpy as np
import dc_model as dc
from app import backtest as bt

LINES = [x / 4 for x in range(-10, 11)]
CODE_W = {"win": (1, 0), "win_half": (0.5, 0), "push": (0, 0), "lose_half": (0, 0.5), "lose": (0, 1)}

def bt_wl(matrix, line):
    W = L = 0.0
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            p = float(matrix[i, j])
            if not p: continue
            win_w = lose_w = 0.0
            for sl, w in bt.quarter_split(line):
                o = bt._half_outcome(i, j, sl, "主")
                win_w += w if o == "win" else 0; lose_w += w if o == "lose" else 0
            W += p * win_w; L += p * lose_w
    return W, L

out = {"per_score": {"n": 0, "mismatch": []}, "matrices": []}
# 1) 单点比分：settle_code 结果码 ↔ dc W/P/L（按子盘数归一）
for i in range(6):
    for j in range(6):
        m = np.zeros((11, 11)); m[i, j] = 1.0
        for ln in LINES:
            code, _ = bt.settle_code(i, j, ln, "主")
            W, P, L = dc.ah_win_push_lose(m, ln); k = len(dc.ah_sub_lines(ln))
            exp = CODE_W[code]
            out["per_score"]["n"] += 1
            if abs(W / k - exp[0]) > 1e-12 or abs(L / k - exp[1]) > 1e-12:
                out["per_score"]["mismatch"].append([i, j, ln, code, W / k, L / k])
# 2) DC 矩阵：q_dc vs q_bt
for lam, mu, rho in ((1.6, 1.1, -0.08), (0.9, 1.4, -0.05), (2.3, 0.7, 0.0), (1.2, 1.2, -0.12)):
    m, rho_used, _ = dc.dc_matrix(lam, mu, rho)
    rows = []
    for ln in (-1.25, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25):
        q_dc = dc.ah_cover_prob(m, ln); W, L = bt_wl(m, ln); q_bt = W / (W + L)
        rows.append({"line": ln, "q_dc": round(q_dc, 10), "q_backtest": round(q_bt, 10), "abs_diff": abs(q_dc - q_bt)})
    out["matrices"].append({"lam": lam, "mu": mu, "rho_used": rho_used, "rows": rows,
                            "max_abs_diff": max(r["abs_diff"] for r in rows)})
out["max_abs_diff_all"] = max(x["max_abs_diff"] for x in out["matrices"])
out["ok"] = not out["per_score"]["mismatch"] and out["max_abs_diff_all"] < 1e-12
print(json.dumps(out, ensure_ascii=False, indent=1))
