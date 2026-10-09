# v2.0 D2 · 副本演练笔记（2026-10-06）

> 拍板见 `v2_0-d2-dual-write-plan.md` §5.1。现网双写 **关**（`DUAL_WRITE_ODDS_ASIAN` 默认 0；API 0.3.11 health 回显）。

## 路径

| 项 | 路径 |
|---|---|
| 副本库 | `api/data/v2d2/app.db`（从 `data/v2d1/app.db` 拷贝，含 D1 时间线） |
| Sync / 迁移脚本 | `scripts/sync_odds_asian_from_snapshot.py` |
| VIEW DDL | `docs/schema/v2_0_d2_view_asian_rule.sql`（仅 open/mid/close） |
| 方案 | `v2_0-d2-dual-write-plan.md` |
| 演练输出 | `backups/v2d2-20261006234800/rehearse.json` |

## 口径摘要

- **legacy→snapshot**：旧 `odds_asian`（非 probe）→ `odds_snapshot`（`channel=rule_legacy`，`market=asian`，`point=phase`）；**不改** `odds_asian`。
- **sync-rule**：`channel=rule` ∧ `point∈{open,mid,close}` → 物理 `odds_asian` **仅 INSERT 缺失 (match_id,book,phase)**；已存在 → SKIP；`extras_json.source=v2_dual_write`。
- **t8/t1**：不进 `odds_asian`；VIEW 亦不投影。
- **缺相**：跳过该 phase，记 `gaps_missing_phase`。
- **现网**：脚本默认拒绝 `data/app.db`；即便 `--i-know-this-is-production` 也要求 `DUAL_WRITE_ODDS_ASIAN=1`。

## 演练计数（v2d2）

| 步骤 | 结果 |
|---|---|
| legacy→snapshot | candidates **1413**，inserted **1413**（二次 0 / skip 1413） |
| rule_legacy asian 快照合计 | **1419**（= 1413 旧行 + D1 探针过夜场已有 6 条 rule_legacy） |
| sync-rule | snapshot 12 行 → **INSERT 12**（探针 FRA–BEL / KOR–UZB 的 rule open/mid/close）；skip 0；gaps 0 |
| 二次 sync | INSERT 0 / SKIP 12（幂等） |
| 旧 177 `odds_asian` 指纹 | **<指纹已移除>** × 1413 行，与现网一致（exclude probe）；演练前后 **unchanged** |
| 探针物理行 | 12（均 `source=v2_dual_write`） |

### VIEW 对照 `v_odds_asian_rule`

- view_rows **12**；与物理 dual_write 行 **12/12 数值一致**（handicap/home_water）。
- 旧 177 不要求与 rule VIEW 数值一致（时钟语义为 rule_legacy）。

## 现网开关

```text
DUAL_WRITE_ODDS_ASIAN=0   # 默认
GET /health → {"dual_write_odds_asian": false, "version": "0.3.11"}
```

API **未**在请求路径调用 sync；旗标仅预留 + health 可见。

## 回退

1. 丢副本：`rm -rf data/v2d2/`  
2. 现网未写 `odds_asian`，无需回滚数据。  
3. 关旗标：不设环境变量或设 `0`；代码默认 False。

## 验收命令

```bash
cd api

# 副本演练（幂等）
.venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/v2d2/app.db

# 指纹（现网只读）
.venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/app.db fp
.venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/v2d2/app.db fp

# 拒绝写现网
.venv/bin/python scripts/sync_odds_asian_from_snapshot.py --db data/app.db   # 应拒绝

# VIEW 抽样
sqlite3 data/v2d2/app.db "select book,phase,handicap,home_water from v_odds_asian_rule limit 5;"

# 健康 / V3
curl -s http://127.0.0.1:8787/health
.venv/bin/python scripts/v3_hash.py
```
