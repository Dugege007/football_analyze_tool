# v1.7 · crown/william 水位档位 → 中点水位（API 0.3.8 · 2026-10-06）

> 口径由用户确认。DDL：`v1_7_water_tier_midpoint.sql`；数据迁移：`scripts/migrate_v1_7_water.py`；公共函数：`app/water.py`；自检：`tests/test_water.py`。
> 结算版本 `settlement_version = ah_v4_water_midpoint`。V3 / macau_close 默认结果不变（见 §6）。

## 1. 含义
- 旧手工 JSON 里 crown/william 的 `home_water / away_water` 存的是**水位档位 t**（0–10，0.5 步进），不是水位；每 0.5 档差 0.025 水。
- **中点水位** `w = 0.70 + 0.05 × t`：4.5→0.925，5→0.95，5.5→0.975，6→1.00，6.5→1.025。
- `t = 0` 表示 w ≤ 0.70，`t = 10` 表示 w ≥ 1.20：两头是**截断值**（censored），中点公式给出的 0.70 / 1.20 只是下限 / 上限。
- macau 不动：库里本来就没有水位，结算按仓库固定口径 0.95（`juice_source=fixed_macau`）。

## 2. 公共函数 `app/water.py`
| 函数 | 说明 |
|---|---|
| `tier_to_mid(t)` | `0.70 + 0.05·t`，Decimal 计算，保留 4 位 |
| `water_to_tier(w)` | `clamp(round_half_up((w − 0.70)/0.025) / 2, 0, 10)` |
| `is_censored_tier(t)` | `t ≤ 0 或 t ≥ 10` |
| `convert_tier_pair(h, a)` | 一行盘口的换算结果 + 标记 + 原始档位（迁移和导入共用） |

**舍入**：用 `Decimal(str(w))` 加 `ROUND_HALF_UP`，不用 Python 的 `round()`。`round()` 是银行家舍入，还会受二进制浮点误差影响：例如 0.7125 → x = 0.5，`round(0.5) = 0`，但正确结果是 0.5 档。

**双向自检**（`python -m pytest tests/test_water.py`，4 passed；`python tests/test_water.py` 打印表）：
- 0..10 每 0.5 档 → 中点 → 档：21 个点**全部还原**。
- 边界：0.60→0，0.69→0，0.70→0，0.7124→0，0.7125→0.5，0.725→0.5，0.7375→1，0.925→4.5，0.9374→4.5，0.9375→5，0.95→5，1.00→6，1.1875→10，1.19→10，1.20→10，1.21→10，1.25→10。

## 3. 存储迁移
**哪些表/列存了档位**：只有 `odds_asian.home_water / away_water`（book ∈ crown, william）。
- `odds_raw.odds_json` 是导入时原始 JSON 的整包副本（含原档位），**不改**，作为审计 / 回退源。
- 其余 odds 表（euro_home / jc_home / jc_hhad）没有水位。

**DDL**（`odds_asian` 新列）：
- `water_src`：`tier_midpoint | actual | NULL`
- `water_censored`：1 = 任一侧截断
- `extras_json`：`{"water_tier_raw":{"home","away"},"censored":{"home","away"},"conversion"}`

**数据**：
- 只换 `book ∈ {crown, william} AND water_src IS NULL AND 有水位` 的行，所以**幂等**：第二次执行换算 0 行。
- `--dry-run` 只统计不写；`--rollback` 按 `extras_json.water_tier_raw` 还原。
- 已在库副本上验证：回退后 `odds_asian` 与迁移前**逐行相同**。

**本次迁移结果**：
| 书 | 换算行数 | censored 行 | censored 侧（主 / 客） | t=0 侧 | t=10 侧 |
|---|---|---|---|---|---|
| crown | 531（open 177 / mid 177 / close 177） | 1（mid） | 2（1 / 1） | 1 | 1 |
| william | 528（open 176 / mid 176 / close 176） | 124（open 47 / mid 39 / close 38） | 147（64 / 83） | 124 | 23 |
| macau | 0（354 行不动，water_src=NULL） | — | — | — | — |
| **合计** | **1059** | **125** | **149** | 125 | 24 |

**导入**（`scripts/import_lib.py`）：
- 以后导入旧手工 JSON 时，crown/william 自动做同样换算和标记。
- phase dict 或 book dict 显式带 `"water_src": "actual"` 时（API 来的真实水位）原样存，标 `water_src=actual`。
- 其它书有水位一律 `actual`；macau 不动。
- 库里缺 v1.7 列时自动补 DDL。
- 已在临时库验证：crown 6/0 → 1.00/0.70，censored 客侧；william 带 actual → 0.93/0.97 原样存。
- `app/db.py` 的 `MIGRATION_PATHS` 已加 v1.7 DDL，重建库时自动加列。

**读接口**：`GET /matches/{id}/odds` 默认用 `odds_asian` 的中点 / 真实水位覆盖 crown/william，并附 `water_src / water_censored / water_tier_raw`；`?raw=true` 返回原始导入 JSON（档位）。

## 4. 结算
- `water_tier_map`（旧公式 0.75+(t−1)×0.05）**废弃**。传入会被忽略，summary 里 `deprecations` 给出提示。
  - 注：旧公式在 1~9 档内与 0.70+0.05t 数值相同，所以 0.3.6 用开关算出的 crown 结果与现在一致；区别在 0 / 0.5 / 9.5 / 10 档不再回落，并且标记截断。
- `macau_*`：固定 0.95，`fixed_macau`（默认，不变）。
- `crown_* / william_* / 其它`：直接用库里水位；`juice_source` 按 `water_src` 标为 `tier_midpoint` 或 `actual`；缺失或异常 → `fallback` 0.95。
- 所下那一侧为截断值时记 reason `water_censored`；可选 `exclude_censored`（`params` 或 `config.extras`，默认 false）为 true 时该注不下，reason `water_censored_excluded`。
- summary 新字段：`juice_tier_midpoint_count`、`water_censored_count`、`water_censored_bet_count`、`water_censored_excluded_count`、`exclude_censored`、`deprecations`。cache metrics 增加 `water_src`、`water_censored`。
- 指纹：`settlement_version` 改为 `ah_v4_water_midpoint`，`bankroll_rules.exclude_censored` 进指纹（替代 water_tier_map）。

## 5. 私有仓特征（已移除） 旧读法 vs 修正版对照（不改仓库脚本）
- 仓库本地副本 `01_分析集/tools/asian_handicap_feature_utils.py` 在 box 上**不存在**。
- 以下按 （私有研读笔记，未公开） 的描述实现：「水位 0.75~1.15 线性映射到 1~9 档；细盘 = 盘口 + (主水档 − 5)/4 × 0.125；压力 = 客水档 − 主水档」，即 `rank = clamp(1 + (w − 0.75)/0.05, 1, 9)`。仓库的取整方式未知，表中保留小数。
- **旧读法（bug）**：把档位 t 当水位送进 water_to_rank → t ≥ 1.15 的全部是 rank 9，t ≤ 0.75 的是 1，压力几乎恒为 0。
- **修正版**：t → 中点水位 → rank（在 1~9 内等于 t）；另列我们的 0–10 tier。
- 脚本：`.venv/bin/python scripts/fcahfinp_compare.py（未公开） --book crown --phase close`。

| match | jc_id | 书/阶段 | 盘口 | 主档t | 客档t | 旧:主rank | 旧:客rank | 旧:细盘 | 旧:压力 | 中点主w | 中点客w | 新:主rank | 新:客rank | 新:细盘 | 新:压力 | 我们tier 主/客 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | 一004 | crown/close | 1.0 | 3 | 5.5 | 9 | 9 | 1.125 | 0 | 0.85 | 0.975 | 3 | 5.5 | 0.9375 | 2.5 | 3/5.5 |
| 8 | 二202 | crown/close | 0.25 | 4 | 5 | 9 | 9 | 0.375 | 0 | 0.9 | 0.95 | 4 | 5 | 0.2188 | 1 | 4/5 |
| 12 | 三203 | crown/close | 0.25 | 3 | 6 | 9 | 9 | 0.375 | 0 | 0.85 | 1 | 3 | 6 | 0.1875 | 3 | 3/6 |
| 16 | 四203 | crown/close | 3.25 | 6 | 3 | 9 | 9 | 3.375 | 0 | 1 | 0.85 | 6 | 3 | 3.2812 | -3 | 6/3 |
| 20 | 五202 | crown/close | 1.0 | 7 | 2 | 9 | 9 | 1.125 | 0 | 1.05 | 0.8 | 7 | 2 | 1.0625 | -5 | 7/2 |
| 26 | 六204 | crown/close | 0.25 | 6 | 3.5 | 9 | 9 | 0.375 | 0 | 1 | 0.875 | 6 | 3.5 | 0.2812 | -2.5 | 6/3.5 |

| match | jc_id | 书/阶段 | 盘口 | 主档t | 客档t | 旧:主rank | 旧:客rank | 旧:细盘 | 旧:压力 | 中点主w | 中点客w | 新:主rank | 新:客rank | 新:细盘 | 新:压力 | 我们tier 主/客 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | 一004 | william/open | 0.75 | 0 | 5.5 | 1 | 9 | 0.625 | 8 | 0.7 | 0.975 | 1 | 5.5 | 0.625 | 4.5 | 0/5.5 |
| 8 | 二202 | william/open | 0.5 | 7 | 0 | 9 | 1 | 0.625 | -8 | 1.05 | 0.7 | 7 | 1 | 0.5625 | -6 | 7/0 |
| 26 | 六204 | william/open | 0.5 | 10 | 0 | 9 | 1 | 0.625 | -8 | 1.2 | 0.7 | 9 | 1 | 0.625 | -8 | 10/0 |

## 6. 实测（data_rev `ib:3|p:…`；V3 哈希 `<指纹已移除>` 不变）
- **默认 macau_close compare（V3 + TEST_V3_MIRROR）与 0.3.7 基线逐点一致**（含资金序列）：
  - V3：n …，bet …，…，期末 …，MDD …
  - MIRROR：bet …，…，期末 …，MDD … / …
  - stack：bet …，…，期末 …，MDD … / …，冲突 …（净 …）
- 因 settlement_version 变化 V3 重算了一次，数字一致；二次 `reused`。
- **MIRROR 中点水位**：
  | settle_book | exclude_censored | bet | pnl | MDD | censored（下注 / 剔除） |
  |---|---|---|---|---|---|
  | crown_close | false / true | … | … | … / … | …（crown close 全库无截断） |
  | william_close | false | … | … | … / … | … / … |
  | william_close | true | … | … | … / … | … / … |
  | william_open | false | … | … | … / … | … / … |
  | william_open | true | … | … | … / … | … / … |

## 7. 回退
1. **只回退数据**：`.venv/bin/python scripts/migrate_v1_7_water.py --rollback`（按 extras 还原档位，清空 water_src 等）。
2. **整库回退**：`cp backups/v1_7-20261006173831/app.db data/app.db`。
3. **代码回退**：`backups/v1_7-20261006173831/{app,scripts,README.md}`。
